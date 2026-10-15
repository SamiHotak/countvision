"""The PUBLIC key that checks CountVision Local licence keys (safe to publish).

Empty until Ezat creates his key pair once on his laptop:

    countvision-cloud licence-keygen

That command writes the PRIVATE key to a folder outside the repository (never commit it) and puts
the matching public key below. Commit this file afterwards. Every installer built after that
accepts only licences signed with that private key. Losing the private key means: new keys for
everybody (keep a backup on a USB stick).
"""

PUBLIC_KEY = ""
