"""Create an isolated PostgreSQL cluster using already installed server binaries."""

import os
import argparse
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
DATA = LOCAL / "postgres"
PORT = os.environ.get("CHECKSTOCK_PG_PORT", "55432")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="Stop this project's cluster only")
    args = parser.parse_args()
    candidates = [Path(p) for p in [os.environ.get("PG_BIN", "")] if p]
    if os.name == "nt":
        candidates += sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"), reverse=True)
    found = shutil.which("initdb")
    if found:
        candidates.append(Path(found).parent)
    pg_bin = next(
        (p for p in candidates if (p / ("initdb.exe" if os.name == "nt" else "initdb")).exists()),
        None,
    )
    if not pg_bin:
        sys.exit("Install PostgreSQL server tools or set PG_BIN to their directory.")
    if args.stop:
        if not DATA.exists():
            sys.exit("No project cluster exists.")
        subprocess.run([str(pg_bin / "pg_ctl"), "-D", str(DATA), "-w", "stop"], check=True)
        return
    LOCAL.mkdir(exist_ok=True)
    env_path = ROOT / ".env"
    if not DATA.exists():
        if env_path.exists():
            sys.exit(".env already exists; use its database or move it before local setup.")
        password = secrets.token_urlsafe(24)
        password_file = LOCAL / "init-password"
        password_file.write_text(password, encoding="utf-8")
        try:
            subprocess.run(
                [
                    str(pg_bin / "initdb"),
                    "-D",
                    str(DATA),
                    "-U",
                    "checkstock",
                    "--pwfile",
                    str(password_file),
                    "--auth=scram-sha-256",
                    "--encoding=UTF8",
                    "--locale=C",
                ],
                check=True,
            )
        finally:
            password_file.unlink(missing_ok=True)
        env_path.write_text(
            f"DATABASE_URL=postgresql+psycopg://checkstock:{password}@127.0.0.1:{PORT}/checkstock\n"
            f"TEST_DATABASE_URL=postgresql+psycopg://checkstock:{password}@127.0.0.1:{PORT}/checkstock_test\n",
            encoding="utf-8",
        )
    sys.path.insert(0, str(ROOT))
    from app.config import Settings
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    settings = Settings(_env_file=env_path)
    url = make_url(settings.database_url)
    local_port = url.port or int(PORT)
    result = subprocess.run(
        [str(pg_bin / "pg_ctl"), "-D", str(DATA), "status"], capture_output=True
    )
    if result.returncode:
        subprocess.run(
            [
                str(pg_bin / "pg_ctl"),
                "-D",
                str(DATA),
                "-l",
                str(LOCAL / "postgres.log"),
                "-o",
                f"-p {local_port} -h 127.0.0.1",
                "-w",
                "start",
            ],
            check=True,
        )
    with create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    ).connect() as conn:
        for name in ["checkstock", "checkstock_test"]:
            if not conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name}
            ):
                conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    print("Local PostgreSQL ready. Credentials saved to .env (excluded from Git).")
    print(f"Stop: & '{pg_bin / 'pg_ctl'}' -D '{DATA}' -w stop")


if __name__ == "__main__":
    main()
