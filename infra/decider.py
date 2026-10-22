"""The private Decider serving path.

`docs/spec/design.md` section 9 fixes the route: the controller signs with
SigV4 for `execute-api`, calls a private REST API through an `execute-api` VPC
endpoint, which forwards through a VPC Link and an internal network load
balancer to a reverse proxy, which forwards to the model server on
`127.0.0.1:8000`. The resource policy admits only the controller runtime role
arriving through that endpoint and denies everything else. Security groups
admit one security identity on the proxy port, the load balancer's own
security group, rather than a subnet range: a CIDR rule would admit any
principal that gains network placement in the isolated subnets, which is a
different boundary from the load balancer. The model server port is never
reachable from the network at all.

The VPC has no NAT gateway and no public subnet, so the Decider host has no
serving-time internet egress (section 9 D6). Neither reachability nor the
integration timeout is verified: PA-04, PA-05 and PF6 stay Pending.
"""

from dataclasses import dataclass

from aws_cdk import Duration
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as targets
from aws_cdk import aws_iam as iam
from constructs import Construct

#: The reverse proxy port the internal NLB reaches.
PROXY_PORT = 8443
#: The model server port. Bound to loopback and never reachable remotely.
MODEL_SERVER_PORT = 8000
MODEL_SERVER_BIND = f"127.0.0.1:{MODEL_SERVER_PORT}"
#: At least the 20 second provider deadline of design.md section 9 D3.
PROVIDER_DEADLINE_SECONDS = 20
API_INTEGRATION_TIMEOUT_SECONDS = 29
DECIDER_PATH = "/v1/systemone"


@dataclass(frozen=True)
class DeciderPath:
    """Every Decider-path construct the rest of the topology binds to."""

    vpc: ec2.Vpc
    endpoint: ec2.InterfaceVpcEndpoint
    proxy_security_group: ec2.SecurityGroup
    load_balancer_security_group: ec2.SecurityGroup
    load_balancer: elbv2.NetworkLoadBalancer
    api: apigateway.RestApi


def _user_data(host_role_name: str) -> ec2.UserData:
    """Bind the model server to loopback and the proxy to the private address.

    Neither process binds a wildcard address: the model server is loopback only
    and the proxy binds the instance's own private address, so the security
    group is defence in depth rather than the only boundary.
    """
    data = ec2.UserData.for_linux()
    data.add_commands(
        "set -euo pipefail",
        "umask 027",
        "install -d -m 0750 /etc/unstuck",
        f"printf 'UNSTUCK_HOST_ROLE=%s\\n' '{host_role_name}' > /etc/unstuck/serving.env",
        f"printf 'UNSTUCK_MODEL_BIND=%s\\n' '{MODEL_SERVER_BIND}'"
        " >> /etc/unstuck/serving.env",
        "printf 'UNSTUCK_PROXY_BIND=%s:%s\\n'"
        " \"$(hostname -I | awk '{print $1}')\""
        f" '{PROXY_PORT}' >> /etc/unstuck/serving.env",
        "printf 'UNSTUCK_SERVING_EGRESS=none\\n' >> /etc/unstuck/serving.env",
    )
    return data


def add_decider_path(
    scope: Construct,
    *,
    vpc_cidr: str,
    region: str,
    decider_ami_id: str,
    decider_instance_type: str,
    host_role: iam.Role,
    controller_role_arn: str,
) -> DeciderPath:
    vpc = ec2.Vpc(
        scope,
        "DeciderVpc",
        ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
        # Availability zones are named explicitly rather than discovered, so
        # synthesis issues no account context lookup at all.
        availability_zones=[f"{region}a", f"{region}b"],
        # No NAT gateway and no public subnet: no serving-time internet egress.
        nat_gateways=0,
        subnet_configuration=[
            ec2.SubnetConfiguration(
                name="Isolated",
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                cidr_mask=24,
            )
        ],
    )
    endpoint = vpc.add_interface_endpoint(
        "ExecuteApiEndpoint",
        service=ec2.InterfaceVpcEndpointAwsService.APIGATEWAY,
        private_dns_enabled=True,
        subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
    )
    proxy_security_group = ec2.SecurityGroup(
        scope,
        "DeciderProxySecurityGroup",
        vpc=vpc,
        allow_all_outbound=False,
        description="Internal NLB to proxy port only; model server never exposed",
    )
    load_balancer_security_group = ec2.SecurityGroup(
        scope,
        "DeciderLoadBalancerSecurityGroup",
        vpc=vpc,
        allow_all_outbound=False,
        description="Internal Decider load balancer; proxy port targets only",
    )
    # The source is the load balancer's security identity, not a subnet range,
    # so network placement inside the isolated subnets grants nothing.
    proxy_security_group.add_ingress_rule(
        load_balancer_security_group,
        ec2.Port.tcp(PROXY_PORT),
        "internal network load balancer security group only",
    )
    load_balancer_security_group.add_egress_rule(
        proxy_security_group,
        ec2.Port.tcp(PROXY_PORT),
        "proxy port on the Decider host only",
    )
    proxy = ec2.Instance(
        scope,
        "DeciderHost",
        vpc=vpc,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        instance_type=ec2.InstanceType(decider_instance_type),
        machine_image=ec2.MachineImage.generic_linux({region: decider_ami_id}),
        security_group=proxy_security_group,
        role=host_role,
        user_data=_user_data(host_role.node.id),
        require_imdsv2=True,
        associate_public_ip_address=False,
        detailed_monitoring=False,
    )
    load_balancer = elbv2.NetworkLoadBalancer(
        scope,
        "DeciderNetworkLoadBalancer",
        vpc=vpc,
        internet_facing=False,
        cross_zone_enabled=True,
        vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        security_groups=[load_balancer_security_group],
    )
    listener = load_balancer.add_listener(
        "ProxyListener", port=PROXY_PORT, protocol=elbv2.Protocol.TCP
    )
    listener.add_targets(
        "ProxyTargets",
        port=PROXY_PORT,
        targets=[targets.InstanceTarget(proxy, PROXY_PORT)],
    )
    link = apigateway.VpcLink(
        scope,
        "DeciderVpcLink",
        vpc_link_name="unstuck-decider",
        targets=[load_balancer],
    )
    api = apigateway.RestApi(
        scope,
        "DeciderApi",
        rest_api_name="unstuck-decider",
        description="Private Decider serving path; AWS_IAM only",
        endpoint_configuration=apigateway.EndpointConfiguration(
            types=[apigateway.EndpointType.PRIVATE], vpc_endpoints=[endpoint]
        ),
        policy=_resource_policy(controller_role_arn, endpoint.vpc_endpoint_id),
        # REQ-SEC-10: the IAM matrix of design.md section 15 covers five roles
        # and no more, so the implicit account-level CloudWatch role is not
        # created. Request logging is an owner decision taken with PF6.
        cloud_watch_role=False,
        deploy_options=apigateway.StageOptions(
            stage_name="private", description="offline synthesis only"
        ),
    )
    integration = apigateway.Integration(
        type=apigateway.IntegrationType.HTTP_PROXY,
        integration_http_method="POST",
        uri=f"http://{load_balancer.load_balancer_dns_name}:{PROXY_PORT}{DECIDER_PATH}",
        options=apigateway.IntegrationOptions(
            connection_type=apigateway.ConnectionType.VPC_LINK,
            vpc_link=link,
            timeout=Duration.seconds(API_INTEGRATION_TIMEOUT_SECONDS),
        ),
    )
    api.root.add_resource("v1").add_resource("systemone").add_method(
        "POST", integration, authorization_type=apigateway.AuthorizationType.IAM
    )
    return DeciderPath(
        vpc=vpc,
        endpoint=endpoint,
        proxy_security_group=proxy_security_group,
        load_balancer_security_group=load_balancer_security_group,
        load_balancer=load_balancer,
        api=api,
    )


def _resource_policy(controller_role_arn: str, endpoint_id: str) -> iam.PolicyDocument:
    """Allow the controller role from the endpoint. Deny everything else."""
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="AllowControllerRoleFromTheEndpointOnly",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(controller_role_arn)],
                actions=["execute-api:Invoke"],
                resources=[f"execute-api:/*/POST{DECIDER_PATH}"],
                conditions={"StringEquals": {"aws:SourceVpce": endpoint_id}},
            ),
            iam.PolicyStatement(
                sid="DenyAnyOtherSourceEndpoint",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*"],
                conditions={"StringNotEquals": {"aws:SourceVpce": endpoint_id}},
            ),
            iam.PolicyStatement(
                sid="DenyAnyOtherPrincipal",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*"],
                conditions={
                    "StringNotEquals": {"aws:PrincipalArn": controller_role_arn}
                },
            ),
        ]
    )
