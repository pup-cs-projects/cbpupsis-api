"""Cross-role code every app needs: the shared domains, the outbox, and the worker.

Auth, users, IAM, audit, and notifications live here rather than in one app
because all three apps sign people in, resolve their permissions, and record
audited events. An app never imports another app, so anything two apps need
belongs in this package.
"""
