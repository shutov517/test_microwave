from pydantic_settings import BaseSettings


class AppConfig(BaseSettings):
    secret_key: str
    crypto_algorithm: str = "HS256"

    class Config:
        env_prefix = "app_"


class RedisConfig(BaseSettings):
    host: str = 'redis'
    port: int = 6379
    password: str = ''
    db: int = 0

    lock_mw_name: str = 'lock-mw'

    class Config:
        env_prefix = "redis_"


app_config = AppConfig()
redis_config = RedisConfig()
