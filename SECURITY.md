# Security policy

openField is a network service: it listens on your local network, runs AI tools on your machine and holds access
keys. Reports that help keep it safe are very welcome.

## Supported versions

Security fixes go into the latest release. Update with the installer (running it again updates the installation)
or `git pull` in your checkout.

| Version | Supported |
|---|---|
| 0.2.x | yes |
| < 0.2 | no |

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on
[jpbraga/open_field](https://github.com/jpbraga/open_field/security/advisories/new). Do not open a public issue.

Include the version, how openField was installed and exposed (localhost only or the LAN), the steps to reproduce
and what an attacker gains. You should get an answer within a week. Once a fix is released, the advisory is
published with credit to you, unless you prefer otherwise.

## Security model

What openField is designed to guarantee, and so what counts as a vulnerability:

- **Loopback access:** requests from the machine itself need no key, but only when the `Host` and `Origin`
  headers show a local request; a web page from another site open in the browser does not get this access.
- **Keys:** requests from other machines need a key. openField stores only the SHA-256 of its own keys
  (`keys.json`, mode 0600) and shows a key once, when it is created. Agent keys cannot manage keys, services or
  settings; admin keys can.
- **Shell services:** services that run commands (`shell`, `builtin`) and services that reference secrets can be
  created or edited only from the machine itself, never with a key from another machine.
- **Links:** upload URLs and output download links carry an HMAC signature tied to one upload or one output, and
  download links expire (`links_hours`). Their tokens are kept out of the access log.
- **Files:** job inputs and outputs stay under the data directory; a request cannot read or write outside it.

Out of scope: the content that models generate, vulnerabilities in third-party tools and models that services
call (report those upstream), and setups that expose openField to the internet without a reverse proxy and TLS.
