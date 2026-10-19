"""In-process / volatile storage backends.

For dev/test and the realtime lane (where the alert source re-fires so
losing in-flight state is acceptable). Production paths use SQLite or
Postgres backends; this tier is the fallback when no persistence is
configured.
"""
