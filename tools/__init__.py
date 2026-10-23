"""Deterministic student tools (Member 2). See tools/registry.py for the authorization boundary."""
from tools._common import get_policy_resolver, set_policy_resolver  # noqa: F401
from tools.eligibility import (check_exam_eligibility, check_placement_eligibility,  # noqa: F401
                               check_supplementary_eligibility)
from tools.registry import AccessDeniedError, UnknownToolError, call_tool, tool_specs  # noqa: F401
from tools.student import (get_attendance, get_backlogs, get_exam_result, get_results,  # noqa: F401
                           get_student_profile)
from tools.what_if import simulate_what_if  # noqa: F401
