# Demo Data Seeding (blueprinted_org)

Seeder script:
- `lcs_mvp/seed/seed_blueprinted_org.py`

## Plan-only

```bash
cd /home/claw/work/project-loom/lcs_mvp
.venv/bin/python seed/seed_blueprinted_org.py --profile blueprinted_org --plan
```

## Reset + medium

```bash
cd /home/claw/work/project-loom/lcs_mvp
.venv/bin/python seed/seed_blueprinted_org.py --profile blueprinted_org --reset --scale medium --seed 42 --yes
```

## Reset + large

```bash
cd /home/claw/work/project-loom/lcs_mvp
.venv/bin/python seed/seed_blueprinted_org.py --profile blueprinted_org --reset --scale large --seed 1337 --yes
```

## Custom counts

```bash
cd /home/claw/work/project-loom/lcs_mvp
.venv/bin/python seed/seed_blueprinted_org.py \
  --profile blueprinted_org \
  --reset --yes \
  --tasks 1800 --workflows 520 --assessments 1100 \
  --seed 1337 --pressure-profile balanced
```

## Notes

- `--plan` does not write.
- `--reset` requires `--yes`.
- Domains are enforced to Phase 1 contract set.
- Tasks are seeded tagless; workflow-only tags are used.
