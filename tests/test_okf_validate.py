from pathlib import Path

import pytest

from rebalancer.okf.validate import validate

FIXTURES = Path(__file__).parent / "fixtures" / "okf"


@pytest.mark.parametrize("bundle", ["acme_retail", "ga4"])
def test_googles_sample_bundles_are_clean(bundle):
    rep = validate(FIXTURES / bundle)
    assert rep.ok and rep.warnings == [] and rep.concepts > 5


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


GOOD = "---\ntype: Table\ntitle: T\n---\n# Schema\n"


def test_hard_errors(tmp_path):
    write(tmp_path, "a.md", "no frontmatter here")
    write(tmp_path, "b.md", "---\ntitle: missing type\n---\n")
    write(tmp_path, "c.md", "---\ntype: [unclosed\n---\n")
    write(tmp_path, "sub/index.md", "---\nokf_version: '0.2'\n---\n# Things\n")
    write(tmp_path, "log.md", "# Log\n\n## 21 Oct 2026\n* thing\n")
    write(tmp_path, "ok.md", GOOD)
    errors = " | ".join(validate(tmp_path).errors)
    assert "a.md: no frontmatter block" in errors
    assert "b.md: frontmatter has no 'type'" in errors
    assert "c.md: frontmatter is not valid YAML" in errors
    assert "sub/index.md: only the bundle-root index.md may have frontmatter" in errors
    assert "log.md: log date heading '21 Oct 2026' isn't YYYY-MM-DD" in errors


def test_root_index_may_declare_the_version(tmp_path):
    write(tmp_path, "index.md", "---\nokf_version: '0.2'\n---\n# Tables\n* [ok](ok.md) - fine\n")
    write(tmp_path, "ok.md", GOOD)
    rep = validate(tmp_path)
    assert rep.ok and rep.warnings == []


def test_soft_warnings(tmp_path):
    write(
        tmp_path,
        "m.md",
        """---
type: Attested Computation
generated: { by: some person, at: 2026-10-21 }
verified: { by: human:rs, at: 2026-10-21T10:00:00Z }
status: finished
stale_after: next week
sources:
  - { title: no resource }
parameters:
  - { name: day }
---
See [missing](/tables/nope.md) and [web](https://example.com).
""",
    )
    warnings = " | ".join(validate(tmp_path).warnings)
    assert "generated.by 'some person' isn't an actor" in warnings
    assert "generated.at must be ISO 8601" in warnings
    assert "status 'finished'" in warnings
    assert "stale_after must be ISO 8601" in warnings
    assert "sources[0] needs a resource" in warnings
    assert "needs a runtime" in warnings
    assert "parameters entries need name and type" in warnings
    assert "no computation file and no '# Computation' body" in warnings
    assert "broken link to /tables/nope.md" in warnings
    assert "example.com" not in warnings  # external links aren't checked
    assert "verified" not in warnings  # the human: verification is well formed
