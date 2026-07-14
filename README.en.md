# MAS Startup Eval

[Русский](README.md) | [English](README.en.md)

A multi-agent system on the Claude API for evaluating the prospects of Russian tech startups (0–3 scale: closed / surviving without growth / growing / leader-exit).

## Data

### Sample size

`Data/Startups.xlsx` — a single file, single sheet, already deduplicated:

- TOP: 168 rows
- NOTOP (control): 184 rows
- Total: 352 rows

### What TOP / NOTOP mean — important nuances

- **TOP** — startups selected by **expert review** for the top tier of a support program in a given yearly cycle. This is NOT the same as the platform's top-50 by numeric rating — expert selection and numeric rank can diverge; that's expected, not a data error.
- **NOTOP** — startups the experts placed "outside the top-50." For the same reason, some of these also carry a high numeric rank — not a bug.
- **`год` (year)** — the year of the specific selection cycle/wave (2022–2025), not the company's founding year.
- The same startup can legitimately take part in different yearly cycles and land in TOP one year, NOTOP another (expert recognition changes year to year).
- The platform's numeric rank is a field of the private master tables only — it's not in `Data/Startups.xlsx`; it's mentioned here purely as methodology context.

### Deduplication: rules and what was done

**Legitimate repeats (not duplicates, left as-is):** the same founder can submit different projects in different years — these are distinct startups from one serial founder, kept as independent rows (e.g., one person with three different projects across 2023/2024/2025).

**True duplicates (removed):** a row was treated as a duplicate when founder+university **and** the project's core/name matched — the same startup re-evaluated across cycles with a diverging outcome level. Conflict resolution rule:

1. If the duplicate crosses the TOP/NOTOP boundary — the **TOP** record wins (top status takes priority regardless of year).
2. If the duplicate is within one group (TOP↔TOP or NOTOP↔NOTOP across years) — the **later year** wins (reflects current status).
3. If verification status differs — the verified record wins.

16 rows were removed in total (2 found while checking TOP/NOTOP cross-overlaps, 14 via an extended founder+university search).

### Fields: what's physically in the data, what agents see, what's hidden

| Column | Meaning | Visible to agents? |
|---|---|---|
| `Name`, `год` | Identifier, selection-cycle year | yes |
| `суть_проекта`, `ключевые_слова_nlp` | Project description (100% filled) | yes |
| `рынок` | Free text on market/regulatory context — **not a ready-made market category** (~94% filled) | yes |
| `инвестиции` | Free text on funding need — **not a fact of funds already raised** (~52% filled) | yes |
| `основатель`, `вуз`, `регион` | Team metadata | yes |
| `pat_rus_*`, `pub_*`, `pat_foreign_*` | Russian patents / scientific publications / foreign patents on the topic (not the startup itself), 2018–2024 | yes |
| `Уровень_исхода_0_3` | Real outcome (0–3) | **no** — physically absent from `Data/Startups.xlsx`, kept in a separate private file for post-run validation |
| `Верифицирован`, `Источник_исхода`, `Комментарий_исхода`, `url` | Outcome-labeling metadata | **no**, private master tables only, never enters the repo |
| Group (top/control) | Sample membership | **no** — private validation file only |

## Structure

```
prompts/     system prompts for the 5 agents (Scout → Technologist/Market/Team → Integrator)
src/         pipeline (src/pipeline.py)
Data/        Startups.xlsx — input table
results/     run output (per-startup JSON + results.csv, not in repo)
```

## Running

```
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY

python src/pipeline.py --input Data/Startups.xlsx --limit 20
```

Flags: `--input` (path to the table), `--output-dir` (default `results`), `--limit` (cap the number of startups for debugging).

## Architecture

Agents never access the internet and never see the real outcome or group label — only the dataset fields explicitly whitelisted per agent in `pipeline.py`. Each agent returns JSON against a fixed schema (`output_config.format`), so parsing is reliable without retries on malformed output.

| Agent | Reads from the table | + |
|---|---|---|
| Scout | Name, суть_проекта, ключевые_слова_nlp, рынок, основатель, вуз, регион, год | — |
| Technologist | pat_rus_\*, pub_\*, pat_foreign_\* | Scout's profile |
| Market | рынок, инвестиции, регион, год | Scout's profile |
| Team | основатель, вуз, регион, год | Scout's profile |
| Integrator | none directly | Scout's profile + all three specialist outputs |

## Validation

`Уровень_исхода_0_3` (the real outcome) and the group label (top/control) live in a separate private file, not under `Data/`. After a run they're checked against the Integrator's score:

- F1, recall, quadratic weighted kappa (QWK — preferred for the ordinal 0–3 scale)
- Comparison of scores between the top-program and control groups (ROC/AUC, Mann–Whitney test) — the study's core hypothesis
- Optional: how many times the same founder appears in top ratings across different years — an extra serial-founder signal

## Known limitations / data quality nuances

- In NOTOP, **all** rows with `Уровень_исхода=0` are simultaneously unverified (100% overlap). A level-0 outcome in the control group functionally means "no independent data found," not a confirmed absence of growth/closure — a calibration risk for the Integrator: the model could learn "few mentions online" instead of "signs of failure."
- Patent/publication fields (`pat_rus_*`, `pat_foreign_*`, `pub_*`) have zero missing values — only legitimate zeros (no patents found for the keyword query).
- The combined TTS score (CAGR + CII + II + methodology-defined Z-normalization) hasn't been computed in any file yet — it's a separate deterministic preprocessing step before feeding data to the Technologist agent, not a ready-made column.
