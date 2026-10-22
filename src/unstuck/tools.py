from pydantic import JsonValue

from unstuck.controller import Controller
from unstuck.domain import EvidenceRecord


class ReportTool:
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, controller: Controller):
        self.controller = controller

    def invoke(
        self, arguments: dict[str, JsonValue]
    ) -> EvidenceRecord | dict[str, JsonValue]:
        if arguments:
            return self.controller.reject_report_arguments()
        return self.controller.execute_report()
