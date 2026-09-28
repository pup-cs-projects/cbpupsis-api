"""Infrastructure with no database: settings, logging, errors, events, email.

The bottom of the dependency graph. Nothing here imports another workspace
member, which is what lets ``cbpupsis_database`` read its settings from here.
"""
