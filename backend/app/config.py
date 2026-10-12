from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    voyage_api_key: str = ""
    voyage_embedding_model: str = "voyage-4-lite"
    voyage_embedding_dimensions: int = 1024

    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    # App-folder access: paths are relative to Dropbox/Apps/<app-name>/
    dropbox_inbox_path: str = "/Inbox"
    # Set DROPBOX_WATCHER_ENABLED=0 to stop the poller starting with the app --
    # useful when working on the frontend, since uvicorn --reload restarts on
    # every save and each restart would re-scan the inbox.
    dropbox_watcher_enabled: bool = True

    sop_base_url: str = "https://sop.utoronto.ca/wp-json"
    sop_campus: str = "St. George"
    sop_user_agent: str = "CampusCompass/0.1"

    # IANA zone of the school, used to read poster/SOP wall-clock times. Times are
    # *stored* as UTC regardless (see app/timeutil.py). An IANA name, not "EST":
    # a fixed offset would be wrong for half the year.
    school_timezone: str = "America/Toronto"

    database_url: str = "sqlite:///./data/campus_compass.db"
    chroma_persist_dir: str = "./data/chroma"

    # --- security -----------------------------------------------------------
    # "development" keeps the transport rules inert so localhost keeps working;
    # anything else turns on the HTTPS redirect, HSTS and host allow-listing.
    # Set ENVIRONMENT=production the moment this is reachable off this machine.
    environment: str = "development"

    # Comma-separated so they can be set from a single env var on any host.
    cors_origins: str = "http://localhost:5173,http://localhost:5500,http://127.0.0.1:5500"
    # Host header allow-list, used only outside development. "*" disables the check.
    trusted_hosts: str = "*"

    # Signs the one-time tokens that let an uploader confirm their own pending events
    # (app/confirm_token.py). Empty = random per process, which is fine for one worker.
    confirm_token_secret: str = ""

    # Rate limiting is per-process and in-memory (see app/security.py), which is
    # correct for one uvicorn worker. Turn it off for load testing, not casually:
    # /recommend spends a Voyage call plus a Sonnet call on every request.
    rate_limit_enabled: bool = True

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() != "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()]


settings = Settings()
