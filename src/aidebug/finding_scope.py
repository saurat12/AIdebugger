"""Separate behavioral hypotheses from quality observations without promoting them."""

QUALITY_CATEGORIES = {"coverage_gap", "coverage", "style", "maintainability", "verbosity", "dead_code",
                      "code_smell", "performance_suggestion", "quality", "code_quality"}
PATTERN_ONLY = {"mutable_default", "bare_except", "coverage_gap", "native_evidence"}


def is_non_bug_hypothesis(item):
    return (item.finding_kind == "observation" or item.category.lower() in QUALITY_CATEGORIES or
            (item.reproduction or {}).get("kind") == "coverage_gap")


def is_bug(finding):
    item = finding.hypothesis
    # Quality/coverage observations are never promoted by executing the same symbol.
    if is_non_bug_hypothesis(item):
        return False
    # A concrete independent reproduction can establish behavior for pattern
    # signals such as mutable defaults, but syntax alone cannot.
    if (item.reproduction or {}).get("kind") in PATTERN_ONLY and not (
        finding.status == "confirmed" and finding.check is not None and not finding.check.passed and item.verification_spec
    ):
        return False
    # A generic mutable-default warning is not evidence of state leakage.
    if "mutable default" in item.description.lower() and not item.behavioral_failure and not item.verification_spec:
        return False
    return True
