"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, confloat, constr


class MatchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="待匹配的外部地址文本")
    save: bool = Field(True, description="是否把匹配结果写入待复核/日志表")


class BatchMatchRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)
    save: bool = True


class AddressIn(BaseModel):
    address: str | None = None
    full_text: str | None = None
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    postcode: str | None = None
    zip: str | None = None
    country: str | None = None
    external_id: str | None = None
    id: str | None = None

    def lookup_fields(self) -> dict[str, Any]:
        return {
            "line1": self.line1,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code or self.postcode or self.zip,
            "country": self.country,
            "external_id": self.external_id or self.id,
        }


class AddressBatchIn(BaseModel):
    addresses: list[AddressIn]
    skip_duplicates: bool = True


class ReviewAction(BaseModel):
    action: str = Field(..., pattern="^(confirmed|overridden|rejected)$")
    selected_id: int | None = None


class GroupReviewAction(BaseModel):
    """整组复核请求：group_key 为组签名，match_ids 为客户端看到的成员集合。"""
    group_key: str = Field(..., min_length=1)
    match_ids: list[int] = Field(..., min_length=1)
    action: str = Field(..., pattern="^(confirmed|overridden|rejected)$")
    selected_id: int | None = None


# ------------------------------------------------------------- A/B experiments


class RuleProfile(BaseModel):
    """一档命名规则档案：判定阈值 + 词元/门牌号权重。"""
    name: constr(strip_whitespace=True, min_length=1, max_length=40) = Field(
        ..., description="档案名称（同一实验内不可重复）"
    )
    auto_threshold: confloat(ge=0.0, le=1.0) = Field(
        ..., description="自动命中阈值，达到该分且领先明显才自动命中"
    )
    reject_threshold: confloat(ge=0.0, le=1.0) = Field(
        ..., description="拒绝阈值，首选低于该分即无匹配"
    )
    margin_threshold: confloat(ge=0.0, le=1.0) = Field(
        ..., description="前两名分差小于该值时判为待确认（歧义）"
    )
    token_weight: confloat(ge=0.0, le=1.0) = Field(
        ..., description="词元相似度权重，门牌号权重为 1 - token_weight"
    )


class ExperimentRequest(BaseModel):
    """规则 A/B 对照实验请求：2~4 个档案 + 1~100 条查询。"""
    profiles: list[RuleProfile] = Field(..., min_length=2, max_length=4)
    queries: list[constr(strip_whitespace=True, min_length=1)] = Field(
        ..., min_length=1, max_length=100
    )


class ExperimentCompareRequest(BaseModel):
    """历史回归对比请求：两次已持久化实验的编号。"""
    base_id: constr(strip_whitespace=True, min_length=1) = Field(
        ..., description="基准实验编号（较早的一次，形如 EXP-...）"
    )
    target_id: constr(strip_whitespace=True, min_length=1) = Field(
        ..., description="对比实验编号（较晚的一次，形如 EXP-...）"
    )
