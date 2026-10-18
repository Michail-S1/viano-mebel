"""Additive, transactional migrations, explicitly enabled after a full backup."""
import uuid
import json


VERSION = 2
DDL = (
    """CREATE TABLE avito_listings (
        id TEXT PRIMARY KEY, avito_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '', attributes TEXT NOT NULL DEFAULT '{}',
        price_cents INTEGER, price_source TEXT NOT NULL, observed_at TEXT,
        received_at TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1, price_review INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE avito_links (
        listing_id TEXT PRIMARY KEY REFERENCES avito_listings(id),
        product_id TEXT NOT NULL REFERENCES products(id),
        product_snapshot TEXT NOT NULL, listing_snapshot TEXT NOT NULL,
        confirmed_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)""",
    """CREATE INDEX idx_avito_links_product ON avito_links(product_id)""",
    """CREATE TABLE avito_decisions (
        id TEXT PRIMARY KEY, listing_id TEXT NOT NULL REFERENCES avito_listings(id),
        product_id TEXT, action TEXT NOT NULL, reason TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE avito_plans (
        id TEXT PRIMARY KEY, factory TEXT NOT NULL, created_at TEXT NOT NULL,
        revision INTEGER NOT NULL, settings TEXT NOT NULL, state TEXT NOT NULL,
        fingerprint TEXT NOT NULL, is_demo INTEGER NOT NULL, invalidated_reason TEXT)""",
    """CREATE TABLE avito_plan_items (
        id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES avito_plans(id),
        listing_id TEXT NOT NULL REFERENCES avito_listings(id),
        product_id TEXT NOT NULL REFERENCES products(id), avito_id TEXT NOT NULL,
        previous_cents INTEGER, target_cents INTEGER,
        price_source TEXT NOT NULL, observed_at TEXT, listing_version INTEGER NOT NULL,
        link_version INTEGER NOT NULL, product_snapshot TEXT NOT NULL,
        state TEXT NOT NULL, reasons TEXT NOT NULL,
        UNIQUE(plan_id,listing_id))""",
    """CREATE TABLE avito_events (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
        listing_id TEXT, details TEXT NOT NULL)""",
)
V2 = ("ALTER TABLE avito_listings ADD COLUMN source_details TEXT NOT NULL DEFAULT '{}'",)


def installed(catalog, db):
    return int(catalog.meta(db, "avito_schema_version") or 0)


def migrate(catalog, error, now):
    with catalog.lock:
        with catalog.connection() as db:
            current = installed(catalog, db)
        if current > VERSION:
            raise error("Раздел Авито создан более новой версией программы.", 409)
        if current == VERSION:
            return {"version": VERSION, "already_installed": True}
        # backup() includes the SQLite snapshot AND every registered catalog photo.
        archive = catalog.backup()
        directory = catalog.data_dir / "backups"
        directory.mkdir(exist_ok=True)
        path = directory / ("before-avito-v" + str(VERSION) + "-" + uuid.uuid4().hex + ".zip")
        with path.open("xb") as output:
            output.write(archive)
        with catalog.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for version in range(current+1, VERSION+1):
                for statement in (DDL if version == 1 else V2):
                    db.execute(statement)
                catalog.set_meta(db, "avito_schema_version", version)
            db.execute("INSERT INTO avito_events VALUES (?,?,?,?,?)",
                       (uuid.uuid4().hex, now(), "migration", None,
                        json.dumps({"version": VERSION, "backup_created": True})))
        return {"version": VERSION, "backup": str(path)}
