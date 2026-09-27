import json

from customs_advisor import CustomsModelResult


def _result(**overrides):
    base = {
        "summary": "Özet",
        "answer_status": "preliminary",
        "candidate_gtips": [{"code": "950300410011", "explanation": "Peluş oyuncak", "confidence": "high"}],
        "controls": [{"name": "TAREKS", "status": "required", "explanation": "Risk esaslı denetim"}],
        "required_documents": [],
        "taxes": [{"name": "İGV", "status": "applicable", "rate": "%25", "explanation": "Ek liste"}],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_valid_values_pass_unchanged():
    parsed = CustomsModelResult.model_validate_json(_result())
    assert parsed.answer_status == "preliminary"
    assert parsed.candidate_gtips[0].confidence == "high"
    assert parsed.controls[0].status == "required"
    assert parsed.taxes[0].status == "applicable"


def test_out_of_list_tax_status_no_longer_fails_whole_result():
    # Canlıda "İstek doğrulanamadı: Input should be 'applicable', 'possible', 'not_found' or 'unknown'"
    # hatasıyla bütün ön değerlendirme düşüyordu.
    parsed = CustomsModelResult.model_validate_json(_result(taxes=[
        {"name": "ÖTV", "status": "not_applicable", "explanation": "Listede yok"},
        {"name": "KDV", "status": "Applies", "rate": "%20", "explanation": "Genel oran"},
        {"name": "Damping", "status": "belirsiz", "explanation": "Doğrulanmadı"},
    ]))
    assert [tax.status for tax in parsed.taxes] == ["not_found", "applicable", "unknown"]


def test_other_choice_fields_fall_back_to_cautious_defaults():
    parsed = CustomsModelResult.model_validate_json(_result(
        answer_status="partial",
        candidate_gtips=[{"code": "95030041", "explanation": "Aday", "confidence": "Yüksek"}],
        controls=[{"name": "CE", "status": "not applicable", "explanation": "—"}],
    ))
    assert parsed.answer_status == "needs_information"
    assert parsed.candidate_gtips[0].confidence == "high"
    assert parsed.controls[0].status == "not_found"


def test_non_string_values_still_fail_validation():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CustomsModelResult.model_validate_json(_result(taxes=[{"name": "İGV", "status": 5, "explanation": "x"}]))
