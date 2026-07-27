import os

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

DATABASE_URL = os.environ["DATABASE_URL"]

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, kwargs={"row_factory": dict_row})
