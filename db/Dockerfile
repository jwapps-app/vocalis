# Postgres with the Vocalis schema baked in.
#
# The schema used to arrive as a bind-mounted init.sql, which silently requires
# the source tree to be present next to the compose file — exactly the
# assumption that breaks a deployment made from compose and .env alone.
FROM postgres:16
COPY init.sql /docker-entrypoint-initdb.d/init.sql
