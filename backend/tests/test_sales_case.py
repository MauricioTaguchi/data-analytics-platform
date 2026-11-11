import hashlib

import pytest

from app.operations.sales_case import SOURCE, reproduce_case


def test_sales_case_proves_before_after_without_changing_its_source():
    original = SOURCE.read_bytes()
    result = reproduce_case()
    assert result["status"] == "passed"
    assert result["synthetic_data"] is True
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert SOURCE.read_bytes() == original
    assert result["before"]["channel_totals_brl"] == {"Online": 1398, "Retail": 1799.8}
    assert result["after"]["channel_totals_brl"] == {"Online": 1398, "Retail": 899.9}
    assert result["before"]["duplicate_rows"] == 1
    assert result["after"]["duplicate_rows"] == 0
    assert result["after"]["recorded_amounts"] == 3
    assert result["after"]["mean_recorded_order_brl"] == pytest.approx(2297.9 / 3)
    assert result["after"]["mean_recorded_order_brl"] != pytest.approx((899.9 + 699) / 2)


def test_sales_case_fails_if_its_documented_evidence_changes(tmp_path):
    changed = tmp_path / "changed.csv"
    changed.write_text(SOURCE.read_text().replace("899.90", "999.90"))
    with pytest.raises(ValueError, match="documented before/after"):
        reproduce_case(changed)
