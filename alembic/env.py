import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from tg_control.config import _read_secret
from tg_control.db import Base

config = context.config
if config.config_file_name and config.get_section("loggers"):
    fileConfig(config.config_file_name)
url_file = os.environ.get("TG_DATABASE_URL_FILE")
url = _read_secret(url_file) if url_file else os.environ["TG_DATABASE_URL"]
# Escape percent signs because ConfigParser interpolates values.
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
