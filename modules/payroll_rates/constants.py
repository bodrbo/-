"""Crew roles for excursion pay rates — a local copy of the three role
keys modules.schedule.constants.CREW_ROLES also uses (captain/guide/
guide_captain), kept separate rather than imported so this module has no
dependency on modules.schedule; modules.schedule is the one that depends
on this module's rates at auto-close time, not the other way around."""

EXCURSION_ROLES = {
    "captain": "Капитан",
    "guide": "Гид",
    "guide_captain": "Гид-капитан",
}

# Seed values — copied from app.py's old WORK_TYPES table, which never
# priced a plain "guide" role differently across trip types (only
# captain-alone vs the combined guide_captain role), so collapsing rates
# down to "one number per role" loses no information that existed before.
# No seed for "guide" — deliberately left at 0 ("не задано") rather than
# guessed; see modules/schedule/services.py's auto-close fallback.
DEFAULT_EXCURSION_ROLE_RATES = {
    "captain": 1100,
    "guide_captain": 1870,
}
