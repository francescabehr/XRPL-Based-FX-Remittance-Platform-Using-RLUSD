from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/remittance_db"
    test_database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/remittance_test"
    secret_key: str = "change-me-before-running"
    debug: bool = False

    admin_email: str = "admin@example.com"
    admin_password: str = "ChangeMe123!"
    admin_name: str = "Platform Admin"
    admin_mobile: str = "+27000000000"

    redis_url: str = "redis://localhost:6379/0"

    # FX rate source: "config" reads the active fee_config row; "static" pins
    # fx_static_market_rate. A live-API source can be added in fx_service without
    # changing any caller.
    fx_rate_source: str = "config"
    fx_static_market_rate: str = "18.50"

    # XRPL / UCTUSD — real values come from .env, never from defaults here.
    xrpl_json_rpc: str = "https://s.altnet.rippletest.net:51234"
    xrpl_issuer_address: str = ""
    # 40-char hex on-ledger currency code; read from the ledger via
    # scripts/check_currency.py and copied verbatim. Never guess it.
    xrpl_currency_code: str = ""
    xrpl_platform_wallet_address: str = ""
    xrpl_platform_wallet_seed: str = ""
    xrpl_encryption_key: str = "change-me-32-byte-hex-key-here"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
