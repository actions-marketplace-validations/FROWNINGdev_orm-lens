# django-orm-lens is now orm-lens

This project was renamed to **[orm-lens](https://pypi.org/project/orm-lens/)** when it started covering SQLAlchemy and Alembic as well as Django.

Switch to the new name:

```bash
pip uninstall django-orm-lens
pip install orm-lens
uvx orm-lens scan
```

Until you do, `pip install django-orm-lens` keeps working and installs the newest orm-lens 1.x:

- the `django-orm-lens` and `django-orm-lens-mcp` commands still run, and print a one-line note on stderr (`ORM_LENS_NO_RENAME_NOTICE=1` hides it);
- `pip install "django-orm-lens[mcp]"` still pulls in the MCP server;
- `import django_orm_lens` still resolves, with a `DeprecationWarning`.

Source, docs and issues: https://github.com/FROWNINGdev/orm-lens
