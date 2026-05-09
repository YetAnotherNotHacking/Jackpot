import os


class Config:
    APP_NAME = os.getenv("APP_NAME", "AceDraw")
    TILE_SIZE = int(os.getenv("TILE_SIZE", "512"))
    TILE_ROOT = os.getenv("TILE_ROOT", "/app/data/tiles")
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql://acedraw:acedraw@localhost:5432/acedraw",
    )
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    MAX_ZOOM = int(os.getenv("MAX_ZOOM", "10"))
    MAX_PROPAGATION_DEPTH = int(os.getenv("MAX_PROPAGATION_DEPTH", "4"))
