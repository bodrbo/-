"""Names and schedule of the database backups."""

BACKUP_PREFIX = "workhours-"
BACKUP_SUFFIX = ".db.gz"
STATUS_FILE = "status.json"
LOCK_FILE = ".lock"

# A copy is made about once a day: preferably at night (server time), and
# whatever the hour once the last one is older than OVERDUE_HOURS.
DUE_HOURS = 20
OVERDUE_HOURS = 30
NIGHT_HOURS = range(2, 6)
# after a failed attempt the next one waits this long
RETRY_AFTER_FAILURE_MINUTES = 60
# a lock older than this belongs to a process that died
STALE_LOCK_MINUTES = 30
# the web process looks at the schedule at most this often
CHECK_EVERY_SECONDS = 600

# retention: the newest N copies, the newest of each of the last W weeks and
# of the last M months
KEEP_RECENT = 14
KEEP_WEEKS = 8
KEEP_MONTHS = 6
