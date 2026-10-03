"""Hunt verification never requests a model-generated reproduction plan."""

from types import SimpleNamespace
from unittest.mock import Mock

from aidebug.hunt import BugHypothesis, hunt_project
from aidebug.hunt_registry import RegistryVerifier
from aidebug.models import ProjectInfo


def test_unstructured_hypothesis_is_retained_without_a_verification_model_call(tmp_path):
    source = "def target(value):\n    return value\n"
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("sample.py", "target", "Possible boundary defect", "No bounded input",
                               .9, "Try unusual values", category="other")
    detector = SimpleNamespace(hunt=lambda project: (hypothesis,))
    agent = SimpleNamespace(_complete=Mock(side_effect=AssertionError("verification must not call a model")))

    result = hunt_project(ProjectInfo(tmp_path, ("python",)), (detector,), RegistryVerifier(), agent, timeout=1)

    agent._complete.assert_not_called()
    assert len(result.findings) == 1
    assert result.findings[0].verification_state == "UNVERIFIABLE"
    assert "free-form reproduction text is never executed" in result.findings[0].evidence
    assert result.findings[0].verification_plan["plan"] is None
    assert not result.repairs
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == source
