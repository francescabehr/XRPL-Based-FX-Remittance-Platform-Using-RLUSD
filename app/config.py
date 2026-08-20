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

    xrpl_encryption_key: str = "change-me-32-byte-hex-key-here"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
