"""Configuração centralizada.

Regra do projeto: **nenhum valor sensível tem default utilizável**. Segredo sem
valor definido faz a aplicação recusar a subir quando `APP_ENV=production`. É
preferível falhar no boot, barulhento, a subir em produção assinando token com
a chave de exemplo que veio no `.env.example`.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Valor-sentinela do .env.example. Se ele chegar em produção, é porque ninguém
# trocou o segredo — e isso precisa ser um erro, não um aviso no log.
INSECURE_PLACEHOLDER = "troque-este-valor"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Aplicação ---------------------------------------------------------
    app_name: str = "Sentinela"
    app_env: Literal["development", "test", "production"] = "development"
    app_version: str = "1.0.0"
    debug: bool = False

    # Em produção a documentação interativa fica fechada por padrão: ela
    # enumera todos os endpoints e schemas para quem não está autenticado.
    docs_enabled: bool = True

    # -- Banco -------------------------------------------------------------
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+psycopg://sentinela:sentinela@localhost:5432/sentinela")
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = True
    db_echo: bool = False

    # -- Autenticação ------------------------------------------------------
    jwt_secret: SecretStr = Field(default=SecretStr(INSECURE_PLACEHOLDER))
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "sentinela"
    jwt_audience: str = "sentinela-api"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_ttl_days: int = Field(default=7, ge=1, le=90)

    # Custo do bcrypt. 12 é o piso recomendado hoje; abaixo disso o hash sai
    # rápido demais para resistir a quebra offline.
    #
    # O mínimo do campo é 4, não 12, porque a suíte de testes precisa de um
    # custo baixo: com 12, cada hash leva ~250 ms e dezenas de logins de
    # teste viram um minuto de espera — o que leva alguém a parar de rodar a
    # suíte. O piso de 12 é exigido **em produção**, no validador abaixo.
    # Assim o valor inseguro é possível onde não custa nada e impossível
    # onde custaria tudo.
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)

    password_min_length: int = Field(default=12, ge=8)

    # -- Bloqueio de conta -------------------------------------------------
    login_max_attempts: int = Field(default=5, ge=3)
    login_lockout_minutes: int = Field(default=15, ge=1)

    # -- Rate limiting -----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = Field(default=120, ge=1)
    rate_limit_login_per_minute: int = Field(default=10, ge=1)

    # -- Serviço externo de notificação ------------------------------------
    notifier_base_url: str = "http://localhost:9090"
    notifier_api_key: SecretStr = Field(default=SecretStr(INSECURE_PLACEHOLDER))
    notifier_timeout_seconds: float = Field(default=5.0, gt=0)
    notifier_max_attempts: int = Field(default=3, ge=1, le=10)
    notifier_backoff_base_seconds: float = Field(default=0.5, gt=0)
    notifier_circuit_failure_threshold: int = Field(default=5, ge=1)
    notifier_circuit_reset_seconds: float = Field(default=30.0, gt=0)

    # -- Automação ---------------------------------------------------------
    scheduler_enabled: bool = True
    # Horário do job diário, em hora local do processo (UTC nos containers).
    daily_job_hour: int = Field(default=3, ge=0, le=23)
    daily_job_minute: int = Field(default=0, ge=0, le=59)
    # Quantos dias antes do vencimento gerar alerta. Cada limiar vira uma
    # notificação distinta e idempotente por contrato.
    # `NoDecode` desliga a decodificação automática do pydantic-settings, que
    # tenta ler todo campo de tipo complexo como JSON e falha em
    # `EXPIRATION_ALERT_DAYS=30,15,7,1` **antes** de qualquer validador rodar.
    # Com ele, o valor chega cru ao `_split_csv` abaixo, que aceita as duas
    # formas. Sem ele, a única sintaxe possível num .env seria `[30,15,7,1]`,
    # que é JSON dentro de arquivo de ambiente — funciona, mas ninguém espera.
    expiration_alert_days: Annotated[list[int], NoDecode] = Field(default=[30, 15, 7, 1])

    # -- Provisionamento inicial -------------------------------------------
    admin_initial_email: str = "admin@sentinela.local"
    admin_initial_password: SecretStr | None = None

    # -- Observabilidade ---------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    # Confiar no cabeçalho X-Forwarded-For só quando existe um proxy reverso
    # de verdade na frente. Confiar sempre permite forjar o IP da auditoria.
    trust_proxy_headers: bool = False

    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("expiration_alert_days")
    @classmethod
    def _sane_alert_days(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("expiration_alert_days não pode ser vazio")
        if any(day < 0 for day in value):
            raise ValueError("expiration_alert_days só aceita valores não negativos")
        return sorted(set(value), reverse=True)

    @field_validator("cors_origins", "expiration_alert_days", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Aceita `A,B,C` e também JSON, que é o formato natural num `.env`."""
        if isinstance(value, str):
            texto = value.strip()
            if texto.startswith("["):
                import json

                return json.loads(texto)
            return [item.strip() for item in texto.split(",") if item.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _forbid_placeholders_in_production(self) -> Settings:
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.jwt_secret.get_secret_value() in (INSECURE_PLACEHOLDER, ""):
            problems.append("JWT_SECRET")
        if len(self.jwt_secret.get_secret_value()) < 32:
            problems.append("JWT_SECRET (mínimo de 32 caracteres)")
        if self.notifier_api_key.get_secret_value() in (INSECURE_PLACEHOLDER, ""):
            problems.append("NOTIFIER_API_KEY")
        if self.bcrypt_rounds < 12:
            problems.append("BCRYPT_ROUNDS precisa ser no mínimo 12 em produção")
        if self.debug:
            problems.append("DEBUG precisa ser false em produção")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS não pode ser '*' em produção")

        if problems:
            raise ValueError(
                "configuração insegura para APP_ENV=production: " + ", ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instância única, resolvida no primeiro acesso.

    O `lru_cache` é o que permite injetar configuração falsa nos testes:
    `get_settings.cache_clear()` e a próxima chamada relê o ambiente.
    """
    try:
        return Settings()
    except ValueError as exc:  # pragma: no cover - caminho de boot
        print(f"[sentinela] configuração inválida: {exc}", file=sys.stderr)
        raise
