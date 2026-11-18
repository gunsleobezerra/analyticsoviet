"""Contract of the administrative configuration API.

The panel is generated from ``ConfigSchema``; the values it shows come from
``ConfigView``. Both are built from ``core.config_contract``, so a field can
only appear here if the registry classifies it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.config_contract import ConfigSource, FieldSpec, Mutability, mask


class FieldState(BaseModel):
    """One field as the panel sees it: value, origin and what an edit implies."""

    key: str = Field(description="Nome do campo, igual à variável de ambiente.")
    section: str = Field(description="Seção da tela à qual o campo pertence.")
    type: Literal["string", "integer", "number", "boolean", "list", "enum", "path"] = Field(
        description="Tipo do valor em JSON; define o controle de entrada."
    )
    value: Any = Field(default=None, description="Valor efetivo; segredo vem mascarado ou nulo.")
    source: ConfigSource = Field(description="De onde veio o valor efetivo: padrão, ambiente ou banco.")
    mutability: Mutability = Field(description="Quando o valor passa a valer e se aceita override.")
    editable: bool = Field(description="Se o painel permite gravar um override para este campo.")
    requires_restart: bool = Field(description="Se o valor novo só vale em processo novo.")
    secret: bool = Field(description="Se o valor é segredo e nunca pode ser devolvido.")
    is_set: bool = Field(default=False, description="Se há valor definido (para segredo, se há segredo gravado).")
    minimum: int | float | None = Field(default=None, description="Menor valor aceito, quando numérico.")
    maximum: int | float | None = Field(default=None, description="Maior valor aceito, quando numérico.")


class ConfigSection(BaseModel):
    """Fields of one section, in declaration order."""

    id: str = Field(description="Identificador da seção.")
    fields: list[FieldState] = Field(default_factory=list, description="Campos da seção.")


class ConfigView(BaseModel):
    """Effective configuration, grouped by section."""

    version: int = Field(description="Versão da configuração no banco; muda a cada escrita.")
    environment: str = Field(description="Ambiente em execução (dev, prod, test).")
    sections: list[ConfigSection] = Field(default_factory=list, description="Seções da tela.")
    requires_restart: list[str] = Field(
        default_factory=list, description="Campos com override gravado que só valem depois de reiniciar."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Avisos de leitura, como override de campo que não existe mais."
    )


class SchemaField(BaseModel):
    """A field description with no value: what the form is built from."""

    key: str = Field(description="Nome do campo, igual à variável de ambiente.")
    section: str = Field(description="Seção da tela à qual o campo pertence.")
    type: Literal["string", "integer", "number", "boolean", "list", "enum", "path"] = Field(
        description="Tipo do valor em JSON; define o controle de entrada."
    )
    mutability: Mutability = Field(description="Quando o valor passa a valer e se aceita override.")
    editable: bool = Field(description="Se o painel permite gravar um override para este campo.")
    secret: bool = Field(description="Se o valor é segredo e nunca pode ser devolvido.")
    default: Any = Field(default=None, description="Valor padrão do código, quando exibível.")
    minimum: int | float | None = Field(default=None, description="Menor valor aceito, quando numérico.")
    maximum: int | float | None = Field(default=None, description="Maior valor aceito, quando numérico.")


class SchemaSection(BaseModel):
    """Fields of one section in the schema document."""

    id: str = Field(description="Identificador da seção.")
    fields: list[SchemaField] = Field(default_factory=list, description="Campos da seção.")


class ConfigSchema(BaseModel):
    """Machine-readable description of every configurable field."""

    version: int = Field(description="Versão do contrato de configuração.")
    sections: list[SchemaSection] = Field(default_factory=list, description="Seções da tela.")


class ConfigPatchRequest(BaseModel):
    """A write of one or more fields; the whole request is applied or none of it."""

    values: dict[str, Any] = Field(
        description="Campo para valor novo. Segredo enviado como '***' é ignorado, não sobrescreve."
    )

    @field_validator("values")
    @classmethod
    def _not_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("informe ao menos um campo")
        return value


class ConfigPatchResult(BaseModel):
    """What a write changed, and what still needs a restart."""

    version: int = Field(description="Versão da configuração depois da escrita.")
    applied: dict[str, Any] = Field(default_factory=dict, description="Valor efetivo dos campos alterados.")
    unchanged: list[str] = Field(default_factory=list, description="Campos enviados que já tinham esse valor.")
    requires_restart: list[str] = Field(
        default_factory=list, description="Campos gravados que só valem após reiniciar."
    )


class ConfigDeleteResult(BaseModel):
    """The state of a field after its override was removed."""

    key: str = Field(description="Campo cujo override foi removido.")
    version: int = Field(description="Versão da configuração depois da remoção.")
    value: Any = Field(default=None, description="Valor efetivo agora, com segredo mascarado.")
    source: ConfigSource = Field(description="Origem do valor efetivo depois da remoção.")


class ValidationIssue(BaseModel):
    """One reason a proposed value was refused."""

    field: str = Field(description="Campo com problema.")
    code: str = Field(description="Código estável do motivo: unknown_key, env_only, read_only, invalid_value.")
    message: str = Field(description="Motivo em texto, para exibir no campo.")


class ValidationResult(BaseModel):
    """Result of validating values without storing them."""

    ok: bool = Field(description="Se todos os valores passaram.")
    issues: list[ValidationIssue] = Field(default_factory=list, description="Motivos de recusa, por campo.")


class ConnectionTestResult(BaseModel):
    """Read-only connectivity probe of one group of fields."""

    group: Literal["s3", "redis", "youtube", "meta"] = Field(description="Grupo testado.")
    ok: bool = Field(description="Se a chamada de leitura respondeu como esperado.")
    detail: str = Field(default="", description="Mensagem do provedor, sem credencial.")


def field_state(
    spec: FieldSpec,
    value: Any,
    source: ConfigSource,
    *,
    is_set: bool,
) -> FieldState:
    """Serialize one field for ``GET /admin/api/config``."""
    return FieldState(
        key=spec.key,
        section=spec.section,
        type=spec.value_type,
        value=mask(spec.key, value, is_set),
        source=source,
        mutability=spec.mutability,
        editable=spec.editable,
        requires_restart=spec.requires_restart,
        secret=spec.secret,
        is_set=is_set,
        minimum=spec.minimum,
        maximum=spec.maximum,
    )


def schema_field(spec: FieldSpec) -> SchemaField:
    """Serialize one field for ``GET /admin/api/config/schema``."""
    return SchemaField(
        key=spec.key,
        section=spec.section,
        type=spec.value_type,
        mutability=spec.mutability,
        editable=spec.editable,
        secret=spec.secret,
        default=None if spec.secret else _jsonable(spec.default),
        minimum=spec.minimum,
        maximum=spec.maximum,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
