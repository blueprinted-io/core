# blueprinted.io: Learning Content System

## Executive Summary

blueprinted.io is a **database-defined learning content system** for technical operations.

It defines work as structured, governed records (Tasks + Workflows) so content does not drift, contradict itself, or rot the moment reality changes.

![blueprinted.io workflow](assets/blueprinted-io-diagram.png)

## Why (LearningOps)

Learning is still built like waterfall software: big launches, slow review cycles, and content that’s obsolete before it’s finished.

**LearningOps** is the alternative: build learning like software.

- break work into atomic units
- compose them into real outcomes
- version + review changes
- ship updates continuously
- keep everything auditable

(Full article: [docs/LearningOps.md](docs/LearningOps.md))

## Live Demo

https://blueprinted-io-mvp.blueprinted.io/login

## Quickstart (Run Locally)

The runnable MVP lives in `lcs_mvp/`.

```bash
cd lcs_mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open: http://127.0.0.1:8000

### Seed the Debian demo dataset

```bash
cd lcs_mvp
source .venv/bin/activate
python3 seed/seed_debian_corpus.py --reset-db
```

### Switch between demo and blank database (admin)

Switch your role to `admin` in the UI, then open:

- http://127.0.0.1:8000/db

### Roles (prototype)

Cookie-based role switching (not real auth):

- `viewer` / `author` / `reviewer`: normal app roles (no audit log)
- `audit`: read-only access, includes audit log
- `admin`: can see everything; can force submit/confirm (recorded in audit)

## Docs

- [Statement of Intent](docs/Statement_of_Intent.md)
- [LearningOps](docs/LearningOps.md)
- [System Design](docs/Learning_Content_System_Design.md)
- [Governance](docs/Learning_Content_Governance.md)
- [Standards context: SCORM, xAPI, cmi5](docs/Standards_SCORM_xAPI_cmi5.md)
- [Auth + Domains (draft)](docs/Auth_and_Domains_Draft.md)

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
