"""Independent pricing contracts must not weaken the existing I12 boundary."""

from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import Field, create_model

import aiteam.types as types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    dimensions = importlib.import_module("check_usage_dimensions")
    registry = importlib.import_module("usage_surface")
finally:
    sys.path.pop(0)


def _replace_spec(monkeypatch, model: str, field: str, **changes) -> None:
    surfaces = []
    for surface in dimensions.PRICING_SURFACES:
        fields = dict(surface.fields)
        if surface.model == model:
            fields[field] = replace(fields[field], **changes)
        surfaces.append(replace(surface, fields=fields))
    monkeypatch.setattr(dimensions, "PRICING_SURFACES", tuple(surfaces))


def _source_root(tmp_path: Path, monkeypatch, source: str) -> Path:
    path = tmp_path / "src" / "aiteam" / "types.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(dimensions, "ROOT", tmp_path)
    return tmp_path


def test_current_pricing_contract_is_green():
    assert dimensions.check_pricing_schema() == []
    assert dimensions.check_pricing_source() == []
    assert dimensions.check_forbidden_units() == []
    assert dimensions.check_pricing_frontend() == []


def test_old_vocabulary_and_surfaces_remain_separate():
    assert set(registry.ALLOWED_DIMENSIONS) == {"token", "count", "duration_ms", "percent"}
    assert registry.PRICING_DIMENSIONS == {
        "token", "count", "money_usd", "usd_per_million", "percent", "duration_ms",
    }
    assert not ({surface.model for surface in registry.PY_SURFACES}
                & {surface.model for surface in registry.PRICING_SURFACES})


@pytest.mark.parametrize("annotation", [Decimal, Decimal | None, list[Decimal], dict[str, Decimal | None]])
def test_decimal_cannot_hide_as_non_numeric(annotation):
    assert dimensions._is_numeric(annotation)


@pytest.mark.parametrize("annotation", [bool, str, Literal[1], Literal["USD"]])
def test_explicit_labels_are_not_numeric(annotation):
    assert not dimensions._is_numeric(annotation)


@pytest.mark.parametrize("name,annotation", [("amount", Decimal), ("score", Decimal), ("score", int)])
def test_renamed_unregistered_pricing_field_is_red(monkeypatch, name, annotation):
    model = create_model("PricingRates", __base__=types.PricingRates, __module__=types.__name__,
                         **{name: (annotation, ...)})
    monkeypatch.setattr(types, "PricingRates", model)
    assert any(f"PricingRates.{name}" in p and "未登记" in p for p in dimensions.check_pricing_schema())


def test_unregistered_text_field_is_also_red(monkeypatch):
    model = create_model("PricingRates", __base__=types.PricingRates, __module__=types.__name__,
                         provenance=(str, ...))
    monkeypatch.setattr(types, "PricingRates", model)
    assert any("provenance" in p and "未登记" in p for p in dimensions.check_pricing_schema())


def test_removed_field_leaves_a_red_registration(monkeypatch):
    monkeypatch.delitem(types.PricingRates.model_fields, "input")
    assert any("PricingRates.input" in p and "不存在" in p for p in dimensions.check_pricing_schema())


def test_removed_class_leaves_a_red_registration(monkeypatch):
    monkeypatch.delattr(types, "PricingRates")
    assert any("PricingRates" in p and "不存在" in p for p in dimensions.check_pricing_schema())


def test_new_pricing_class_requires_its_own_registration(monkeypatch):
    monkeypatch.setattr(types, "PricingExtra", create_model("PricingExtra", value=(str, ...)), raising=False)
    assert any("PricingExtra" in p and "未登记" in p for p in dimensions.check_pricing_schema())


def test_duplicate_pricing_registration_is_red(monkeypatch):
    monkeypatch.setattr(dimensions, "PRICING_SURFACES",
                        dimensions.PRICING_SURFACES + dimensions.PRICING_SURFACES[:1])
    assert any("重复" in p for p in dimensions.check_pricing_schema())


def test_price_dimension_cannot_expand_to_unreviewed_units(monkeypatch):
    _replace_spec(monkeypatch, "PricingRates", "input", dimension="money_cny")
    assert any("PricingRates.input" in p and "独立白名单" in p for p in dimensions.check_pricing_schema())


def test_price_cannot_be_declared_as_a_non_numeric_label(monkeypatch):
    _replace_spec(monkeypatch, "PricingRates", "input", dimension="non_numeric", note="Pretend label")
    assert any("PricingRates.input" in p and "类型非数值" in p for p in dimensions.check_pricing_schema())


def test_non_numeric_exemption_requires_a_reason(monkeypatch):
    _replace_spec(monkeypatch, "PricingCatalog", "version", note="")
    assert any("PricingCatalog.version" in p and "具名" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("annotation", [Any, dict[str, Any], types.Task])
def test_non_numeric_fields_cannot_hide_unregistered_content(monkeypatch, annotation):
    monkeypatch.setattr(types.PricingCatalog.model_fields["version"], "annotation", annotation)
    assert any("PricingCatalog.version" in p and "类型非数值" in p for p in dimensions.check_pricing_schema())


def test_extra_fields_must_be_forbidden(monkeypatch):
    monkeypatch.setitem(types.PricingRates.model_config, "extra", "allow")
    assert any("PricingRates" in p and "extra" in p for p in dimensions.check_pricing_schema())


def test_computed_fields_cannot_bypass_registration(monkeypatch):
    monkeypatch.setitem(types.PricingRates.model_computed_fields, "measurement", object())
    assert any("PricingRates" in p and "计算字段" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("alias", ["alias", "validation_alias", "serialization_alias"])
def test_aliases_cannot_bypass_field_registration(monkeypatch, alias):
    monkeypatch.setattr(types.PricingRates.model_fields["input"], alias, "measurement")
    assert any("PricingRates.input" in p and "别名" in p for p in dimensions.check_pricing_schema())


def test_numeric_type_must_match_its_registered_dimension(monkeypatch):
    monkeypatch.setattr(types.PricingRates.model_fields["input"], "annotation", str)
    assert any("PricingRates.input" in p and "类型不符" in p for p in dimensions.check_pricing_schema())


def test_numeric_metadata_cannot_drift(monkeypatch):
    monkeypatch.setattr(types.PricingRates.model_fields["input"], "json_schema_extra", {"dimension": "count"})
    assert any("PricingRates.input" in p and "元数据" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("metric", ["ctx_last", "ctx_watermark", "native_activity", ""])
def test_pricing_tokens_require_usage_sum_in_registry(monkeypatch, metric):
    _replace_spec(monkeypatch, "PricingRequestLine", "input_tokens", metric=metric)
    assert any("input_tokens" in p and "单请求 usage_sum" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("metric", ["ctx_last", "native_activity"])
def test_pricing_tokens_require_usage_sum_on_schema_field(monkeypatch, metric):
    monkeypatch.setattr(types.PricingRequestLine.model_fields["input_tokens"], "json_schema_extra",
                        {"dimension": "token", "metric": metric})
    assert any("input_tokens" in p and "单请求 usage_sum" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("field,annotation", [("amount", int), ("score", Decimal), ("score", list[Decimal])])
def test_old_model_cannot_add_an_amount_or_hide_it_with_decimal(monkeypatch, field, annotation):
    model = create_model("Task", __base__=types.Task, **{field: (annotation, ...)})
    monkeypatch.setattr(types, "Task", model)
    assert any(f"Task.{field}" in p and "旧模型" in p for p in dimensions.check_pricing_schema())


def test_legacy_surface_renamed_decimal_field_is_still_checked(monkeypatch):
    model = create_model("TokenAttribution", __base__=types.TokenAttribution, measurement=(Decimal, ...))
    monkeypatch.setattr(types, "TokenAttribution", model)
    assert any("measurement" in p and "未申报" in p for p in dimensions.check_python_schema())


def test_old_model_cannot_import_money_metadata(monkeypatch):
    model = create_model("Task", __base__=types.Task,
                         score=(int, Field(json_schema_extra={"dimension": "money_usd"})))
    monkeypatch.setattr(types, "Task", model)
    assert any("Task.score" in p and "旧模型" in p for p in dimensions.check_pricing_schema())


def test_non_numeric_amount_label_is_not_misclassified(monkeypatch):
    model = create_model("Task", __base__=types.Task, amount_description=(str, ...))
    monkeypatch.setattr(types, "Task", model)
    assert not any("Task.amount_description" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("annotation", [types.PricingQuoteItem, list[types.PricingQuoteItem],
                                         dict[str, types.PricingQuoteItem | None]])
def test_old_model_cannot_wrap_a_registered_price(monkeypatch, annotation):
    model = create_model("Task", __base__=types.Task, quote=(annotation, ...))
    monkeypatch.setattr(types, "Task", model)
    assert any("Task.quote" in p and "重导出" in p for p in dimensions.check_pricing_schema())


def test_runtime_reexport_under_an_innocent_name_is_red(monkeypatch):
    monkeypatch.setattr(types, "UsageDetail", types.PricingQuoteItem, raising=False)
    assert any("UsageDetail" in p and "重导出" in p for p in dimensions.check_pricing_schema())


@pytest.mark.parametrize("source", [
    "class PricingUnknown(BaseModel):\n    score: str\n",
    "class PricingRates(BaseModel):\n    unregistered: Decimal\n",
    "class PricingRates(PricingQuoteItem):\n    pass\n",
    "class PricingRates(BaseModel):\n    pass\nclass PricingRates(BaseModel):\n    pass\n",
    "class Wrapper:\n    class PricingRates(BaseModel):\n        pass\n",
    "Alias = PricingQuoteItem\n",
    "from elsewhere import PricingQuoteItem as Alias\n",
    "class Task(BaseModel):\n    quote: PricingQuoteItem\n",
    "class Task(BaseModel):\n    quote: list['PricingQuoteItem']\n",
])
def test_ast_rejects_unregistered_classes_fields_and_reexports(source):
    assert dimensions._pricing_source_contract(source)[0]


def test_ast_allows_only_registered_pricing_contract_references():
    source = '''class PricingQuoteItem(BaseModel):
    rate_record: PricingRateRecord | None
    amount_usd: Decimal | None = Field(json_schema_extra={"dimension": "money_usd"})

    def validated(self) -> PricingQuoteItem:
        return self
'''
    assert dimensions._pricing_source_contract(source)[0] == []


@pytest.mark.parametrize("source", [
    "class Task(BaseModel):\n    cost_usd: Decimal\n",
    "class Task(BaseModel):\n    totalCost: float\n",
    "class Task(BaseModel):\n    unit: str = 'USD'\n",
    "class PricingQuoteItem(BaseModel):\n    undeclared_usd: Decimal\n",
    "class PricingQuoteItem(BaseModel):\n    amount_usd: Decimal = other.cost\n",
    "class PricingQuoteItem(BaseModel):\n    def helper(self):\n        return other.amount_usd\n",
    "class PricingQuoteItem(BaseModel):\n    def helper(self):\n        return self.cost_usd\n",
])
def test_money_scanner_exceptions_do_not_cover_the_class_or_file(tmp_path, monkeypatch, source):
    _source_root(tmp_path, monkeypatch, source)
    assert dimensions.check_forbidden_units()


def test_registered_price_field_and_its_self_reference_are_allowed(tmp_path, monkeypatch):
    _source_root(tmp_path, monkeypatch, '''class PricingQuoteItem(BaseModel):
    amount_usd: Decimal | None = Field(json_schema_extra={"dimension": "money_usd"})
    def helper(self):
        return self.amount_usd
''')
    assert dimensions.check_forbidden_units() == []


def test_registered_nested_field_reference_and_validator_name_are_allowed(tmp_path, monkeypatch):
    _source_root(tmp_path, monkeypatch, '''class PricingQuoteItem(BaseModel):
    amount_usd: Decimal | None = Field(json_schema_extra={"dimension": "money_usd"})
    @model_validator(mode="after")
    def validate_price_status(self):
        return self
class PricingQuoteResponse(BaseModel):
    items: list[PricingQuoteItem]
    def helper(self):
        priced = [item for item in self.items]
        return [item.amount_usd for item in priced]
''')
    assert dimensions.check_forbidden_units() == []


@pytest.mark.parametrize("setup", [
    "priced = other",
    "priced = [item for item in self.items]\n        priced = other",
    "priced = [item for item in self.items]\n        if flag:\n            priced = other",
])
def test_unknown_or_rebound_nested_receiver_is_not_exempted(tmp_path, monkeypatch, setup):
    _source_root(tmp_path, monkeypatch, f'''class PricingQuoteItem(BaseModel):
    amount_usd: Decimal | None = Field(json_schema_extra={{"dimension": "money_usd"}})
class PricingQuoteResponse(BaseModel):
    items: list[PricingQuoteItem]
    def helper(self):
        {setup}
        return [item.amount_usd for item in priced]
''')
    assert dimensions.check_forbidden_units()


def test_unregistered_method_name_is_not_exempted(tmp_path, monkeypatch):
    _source_root(tmp_path, monkeypatch, '''class PricingQuoteItem(BaseModel):
    def export_cost(self):
        return 1
''')
    assert dimensions.check_forbidden_units()


def test_frontend_has_no_pricing_exception(tmp_path, monkeypatch):
    _source_root(tmp_path, monkeypatch, "class Task(BaseModel):\n    pass\n")
    frontend = tmp_path / "dashboard" / "src" / "PricingQuote.tsx"
    frontend.parent.mkdir(parents=True)
    frontend.write_text("export const amount_usd = 'USD';\n", encoding="utf-8")
    assert any("PricingQuote.tsx" in p for p in dimensions.check_forbidden_units())


@pytest.mark.parametrize("annotation,default", [(Decimal, ...), (Decimal | None, Decimal(0))])
def test_incomplete_quote_total_must_support_null(monkeypatch, annotation, default):
    field = types.PricingQuoteResponse.model_fields["total_usd"]
    monkeypatch.setattr(field, "annotation", annotation)
    monkeypatch.setattr(field, "default", default)
    assert any("不完整报价" in p for p in dimensions.check_pricing_schema())


def _pricing_frontend(tmp_path, monkeypatch, source, identifiers=None, interfaces=()):
    _source_root(tmp_path, monkeypatch, "class Task(BaseModel):\n    pass\n")
    rel = "dashboard/src/api/accountUsage.ts"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    surface = registry.PricingFrontendSurface(rel, identifiers or {}, interfaces)
    monkeypatch.setattr(dimensions, "PRICING_FRONTEND_SURFACES", (surface,))
    monkeypatch.setattr(dimensions, "PRICING_I18N_FIELDS", {})
    return target


def test_registered_frontend_schema_matches_every_shared_field(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, '''export interface PricingAccount {
      account_key: string;
      label: string;
      created_at: string;
    }''', {"PricingAccount": registry.FieldSpec("non_numeric", note="Shared account schema")}, ("PricingAccount",))
    assert dimensions.check_pricing_frontend() == []


@pytest.mark.parametrize("field", ["measurement: number;", "measurement: string;", "amount: number;"])
def test_frontend_renamed_or_string_amount_cannot_bypass_complete_schema(tmp_path, monkeypatch, field):
    _pricing_frontend(tmp_path, monkeypatch, f'''export interface PricingAccount {{
      account_key: string;
      label: string;
      created_at: string;
      {field}
    }}''', {"PricingAccount": registry.FieldSpec("non_numeric", note="Shared account schema")}, ("PricingAccount",))
    assert any("字段登记不一致" in p for p in dimensions.check_pricing_frontend())


def test_missing_frontend_schema_field_is_red(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "interface PricingAccount { account_key: string; }",
                      {"PricingAccount": registry.FieldSpec("non_numeric", note="Shared account schema")},
                      ("PricingAccount",))
    assert any("缺失" in p for p in dimensions.check_pricing_frontend())


def test_undeclared_frontend_schema_is_red(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "interface Other { amount: number; }")
    assert any("Other" in p and "未具名登记" in p for p in dimensions.check_pricing_frontend())


def test_new_account_page_does_not_receive_a_whole_file_money_exemption(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "const amount_usd = 1; const secret_cost = 2;",
                      {"amount_usd": registry.FieldSpec("money_usd")})
    problems = dimensions.check_forbidden_units()
    assert any("secret_cost" in p for p in problems)
    assert not any("'amount_usd'" in p for p in problems)


def test_account_frontend_money_permission_does_not_spread_to_old_page(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "const amount_usd = 1;",
                      {"amount_usd": registry.FieldSpec("money_usd")})
    path = tmp_path / "dashboard/src/Legacy.tsx"
    path.write_text("const amount_usd = 1;", encoding="utf-8")
    assert any("Legacy.tsx" in p for p in dimensions.check_forbidden_units())


def test_pricing_model_reference_outside_account_files_is_red(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "export const ready = true;")
    path = tmp_path / "dashboard/src/Legacy.tsx"
    path.write_text("export type Legacy = PricingAccount;", encoding="utf-8")
    assert any("Legacy.tsx" in p for p in dimensions.check_pricing_frontend())


def test_unregistered_token_name_on_account_page_is_red(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "const custom_tokens = 1;")
    assert any("custom_tokens" in p for p in dimensions.check_pricing_frontend())


def test_stale_frontend_identifier_registration_is_red(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "const ready = true;",
                      {"amount_usd": registry.FieldSpec("money_usd")})
    assert any("已无人使用" in p for p in dimensions.check_pricing_frontend())


def test_account_dimensions_stay_attached_to_numeric_fields():
    for model, field, dimension in [
        (types.PricingQuotaSnapshot, "used_percent", "percent"),
        (types.PricingQuotaSnapshot, "window_duration_ms", "duration_ms"),
        (types.PricingAccountEstimate, "delta_used_percent", "percent"),
        (types.PricingAccountEstimate, "estimated_full_week_usd", "money_usd"),
    ]:
        assert model.model_fields[field].json_schema_extra["dimension"] == dimension


def test_frontend_schema_index_signature_is_not_a_registration(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, '''export interface PricingAccount {
      account_key: string; label: string; created_at: string;
      [key: string]: unknown;
    }''', {"PricingAccount": registry.FieldSpec("non_numeric", note="Shared account schema")}, ("PricingAccount",))
    assert any("index signature" in p for p in dimensions.check_pricing_frontend())


def test_frontend_pricing_alias_requires_explicit_registration(tmp_path, monkeypatch):
    _pricing_frontend(tmp_path, monkeypatch, "export type Friendly = PricingAccount;",
                      {"PricingAccount": registry.FieldSpec("non_numeric", note="Shared account schema")})
    assert any("Friendly" in p and "未具名" in p for p in dimensions.check_pricing_frontend())


def _translation_fixture(tmp_path, monkeypatch, source):
    _source_root(tmp_path, monkeypatch, "class Task(BaseModel):\n    pass\n")
    path = tmp_path / "dashboard/src/i18n/en.ts"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(dimensions, "PRICING_FRONTEND_SURFACES", ())
    monkeypatch.setattr(dimensions, "PRICING_I18N_FIELDS", {"dashboard/src/i18n/en.ts": {
        "accountUsage.sampleCost": {
            "sampleCost": registry.FieldSpec("non_numeric", note="Message key"),
            "cost": registry.FieldSpec("non_numeric", note="Message word"),
        },
    }})


def test_exact_account_translation_message_is_allowed(tmp_path, monkeypatch):
    _translation_fixture(tmp_path, monkeypatch, "const en = { accountUsage: { sampleCost: 'Sample cost' } };")
    assert dimensions.check_forbidden_units() == []
    assert dimensions.check_pricing_frontend() == []


@pytest.mark.parametrize("extra", ["other: { sampleCost: 'Other cost' },", "cost: 3,"])
def test_account_translation_permission_does_not_spread_to_old_namespace(tmp_path, monkeypatch, extra):
    _translation_fixture(tmp_path, monkeypatch,
                         f"const en = {{ accountUsage: {{ sampleCost: 'Sample cost' }}, {extra} }};")
    assert dimensions.check_forbidden_units()


def test_account_translation_permission_does_not_spread_to_sibling_message(tmp_path, monkeypatch):
    _translation_fixture(tmp_path, monkeypatch,
                         "const en = { accountUsage: { sampleCost: 'Sample cost', extra: 'cost' } };")
    assert dimensions.check_forbidden_units()


def test_registered_translation_does_not_allow_new_currency_words(tmp_path, monkeypatch):
    _translation_fixture(tmp_path, monkeypatch,
                         "const en = { accountUsage: { sampleCost: 'Sample cost in USD' } };")
    assert any("USD" in p for p in dimensions.check_forbidden_units())


def test_removed_translation_message_leaves_a_red_registration(tmp_path, monkeypatch):
    _translation_fixture(tmp_path, monkeypatch, "const en = { accountUsage: {} };")
    assert any("缺失" in p for p in dimensions.check_pricing_frontend())


def test_monitor_defaults_are_disabled_and_the_interval_is_milliseconds():
    settings = types.PricingMonitorSettings()
    assert settings.enabled is False
    assert settings.interval_ms == 1_800_000
    spec = next(s for s in dimensions.PRICING_SURFACES if s.model == "PricingMonitorSettings")
    assert spec.fields["interval_ms"].dimension == "duration_ms"
    assert types.PricingMonitorSettings.model_fields["interval_ms"].json_schema_extra == {"dimension": "duration_ms"}


def test_monitor_state_cannot_smuggle_an_unregistered_amount(monkeypatch):
    model = create_model("PricingMonitorState", __base__=types.PricingMonitorState,
                         __module__=types.__name__, measurement=(Decimal, ...))
    monkeypatch.setattr(types, "PricingMonitorState", model)
    assert any("PricingMonitorState.measurement" in p and "未登记" in p
               for p in dimensions.check_pricing_schema())


def test_registered_monitor_default_factory_matches_its_field_type():
    source = """class PricingMonitorState(BaseModel):
    settings: PricingMonitorSettings = Field(default_factory=PricingMonitorSettings)
"""
    assert dimensions._pricing_source_contract(source)[0] == []


def test_registered_monitor_default_factory_cannot_construct_another_model():
    source = """class PricingMonitorState(BaseModel):
    settings: PricingMonitorSettings = Field(default_factory=PricingRates)
"""
    assert any("PricingRates" in p for p in dimensions._pricing_source_contract(source)[0])
