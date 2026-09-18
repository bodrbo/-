def list_tenants(db):
    return db.execute(
        "SELECT * FROM demo_tenants ORDER BY created_at DESC, id DESC"
    ).fetchall()


def get_tenant(db, tenant_id):
    return db.execute(
        "SELECT * FROM demo_tenants WHERE id = ?", (tenant_id,)
    ).fetchone()


def get_tenant_by_username(db, username):
    return db.execute(
        "SELECT * FROM demo_tenants WHERE username = ?", (username,)
    ).fetchone()


def slug_exists(db, slug):
    return db.execute(
        "SELECT 1 FROM demo_tenants WHERE slug = ?", (slug,)
    ).fetchone() is not None


def username_exists(db, username, exclude_id=None):
    if exclude_id is not None:
        return db.execute(
            "SELECT 1 FROM demo_tenants WHERE username = ? AND id != ?",
            (username, exclude_id),
        ).fetchone() is not None
    return db.execute(
        "SELECT 1 FROM demo_tenants WHERE username = ?", (username,)
    ).fetchone() is not None


def create_tenant(
    db, slug, company_name, logo_filename, accent_color, enabled_modules,
    username, password_hash, db_path, created_at,
):
    cur = db.execute(
        "INSERT INTO demo_tenants "
        "(slug, company_name, logo_filename, accent_color, enabled_modules, "
        "username, password_hash, db_path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, company_name, logo_filename, accent_color, enabled_modules,
         username, password_hash, db_path, created_at),
    )
    db.commit()
    return cur.lastrowid


def update_tenant(
    db, tenant_id, company_name, logo_filename, accent_color, enabled_modules,
    username, password_hash,
):
    """password_hash is None to leave the existing password untouched —
    only overwritten when the admin actually typed a new one."""
    if password_hash is not None:
        db.execute(
            "UPDATE demo_tenants SET company_name = ?, logo_filename = ?, "
            "accent_color = ?, enabled_modules = ?, username = ?, password_hash = ? "
            "WHERE id = ?",
            (company_name, logo_filename, accent_color, enabled_modules,
             username, password_hash, tenant_id),
        )
    else:
        db.execute(
            "UPDATE demo_tenants SET company_name = ?, logo_filename = ?, "
            "accent_color = ?, enabled_modules = ?, username = ? WHERE id = ?",
            (company_name, logo_filename, accent_color, enabled_modules,
             username, tenant_id),
        )
    db.commit()


def delete_tenant(db, tenant_id):
    db.execute("DELETE FROM demo_tenants WHERE id = ?", (tenant_id,))
    db.commit()
