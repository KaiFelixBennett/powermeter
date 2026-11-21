# Security Policy

powermeter is a local measurement tool. It opens **no listening ports** and never sends
measurements anywhere on its own. The only network activity is:

- **outgoing** reads from devices you configure (smart plugs, `http` sources on your LAN),
- **outgoing** SSH sessions started by `powermeter sync` on the collector, towards hosts
  you configured there.

## What to know before you expose anything

- `powermeter sync` authenticates with your existing SSH setup. Do not commit private keys
  or hostnames you would rather keep private into `config.json` if you share the folder
  (Syncthing, git, cloud drives).
- If you point a device's `http` source at a plug, that URL may contain a token
  (`?user=…&password=…`). Treat `config.json` like a credential file: it lives in your
  user data directory, and it is not uploaded anywhere by this tool.
- The CSV history in `data/` reveals when a machine was on, idle and busy. Share it
  deliberately.

## Reporting a vulnerability

Please open a private report through GitHub's "Report a vulnerability" form on the
repository (Security tab) instead of a public issue. Expect a first response within a
few days; disclosures are handled quietly and a security note is added to
[CHANGELOG.md](CHANGELOG.md) when a fix ships.
