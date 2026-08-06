"""Register PyMySQL as the MySQLdb driver.

Shared hosting (cPanel) rarely has the build tools that `mysqlclient` needs,
so the pure-Python PyMySQL driver is used instead. Django's MySQL backend
imports `MySQLdb`, so it has to be aliased before any model is loaded.
Harmless when the project runs on SQLite or PostgreSQL.
"""

try:
    import pymysql
except ImportError:  # pragma: no cover - MySQL is optional
    pass
else:
    pymysql.install_as_MySQLdb()
