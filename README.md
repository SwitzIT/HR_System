# Switz Group Talent Acquisition (TalentFlow): AI JD Generator + ATS

An end-to-end recruitment web app with two sides:

- **Careers site + candidate portal** — browse open roles, register, apply with a PDF/DOCX resume, track application status, withdraw.
- **HR Talent Console** — raise a requisition, let the AI agent draft the Job Description, review/edit/approve it (human in the loop), publish it, and get every incoming resume automatically screened, scored, ranked and shortlisted against that JD.

Built on the idea in [Rajeev99553/Application-Tracking-System](https://github.com/Rajeev99553/Application-Tracking-System) (Streamlit + Gemini, one resume vs one pasted JD), extended into a multi-user system with a database, with the AI rebuilt on LangChain and Hugging Face.

---

## 1. AI-based Job Description creation

| Step | Who | What happens |
|---|---|---|
| Requisition | HR | A structured form: title, department, industry, city/country, employment type, salary (structure, amount or range, currency, frequency), experience and what it refers to, mandatory skills, education and certifications (each with an explicit *Not applicable*), plus optional details. The app validates every mandatory field first and highlights exactly which ones are missing or conflicting. |
| Draft | AI + app | The AI (your JD prompt in `ai/prompts.py`) writes **Job Overview** and **Key Responsibilities**. The app writes **Required Qualifications & Skills**, **Preferred Qualifications & Skills** (only if supplied) and **Position Details** straight from the confirmed requisition, so requirements, salary, location and experience can't be invented or changed. Replies without the two AI sections, or with clarification questions, are rejected and the previous draft is kept. |
| Review | HR | Markdown editor with preview, "Refine with AI", version history. A **Check before approving** box lists lines that need a look: internal notes or placeholders, a reporting line or leadership duties not in the requisition, technologies nobody asked for, numeric targets. |
| Approve | HR | Approval requires ticking *"I've reviewed this JD for accuracy and fairness"*. Optional maker-checker (`REQUIRE_DIFFERENT_APPROVER=true`). Approver and time are recorded. |
| Publish | System | Only approved JDs are visible. Editing a published JD sends it back to review. |

## 2. ATS: background screening, HR posts the status

**Candidates submit instantly.** The application and resume are saved and the candidate sees **Pending**. A background worker (one per app process, `screening_queue.py`) then screens queued resumes one at a time, oldest first, so a slow model on a CPU never keeps a candidate waiting. Interrupted screenings are re-queued on restart; a resume that crashes the model is marked *Screening failed* and the queue carries on. `flask --app app screen-pending` screens the queue by hand.

**HR sees the result first.** Each resume gets a score:

```
final score = 70% × AI score + 30% × keyword score      (AI_WEIGHT, configurable)

keyword score = 60% must-have skill coverage  (with aliases: PowerBI = Power BI, k8s = Kubernetes …)
              + 15% nice-to-have coverage
              + 15% experience fit (years found vs minimum)
              + 10% JD ↔ resume similarity (Hugging Face sentence embeddings, or word overlap)
```

At or above the job's threshold the AI *suggests* Shortlisted (internal stage only). The pipeline shows the screening progress (it refreshes itself), the score, the AI recommendation, the internal **stage** and a **Candidate sees** column.

**Candidates only see what HR posts.** On a candidate page, **Save** keeps a stage change internal; **Save & post** (or **Post "…" to candidate**) makes it visible. On the pipeline, tick candidates and **Post selected to candidates**. The dashboard lists jobs with statuses waiting to be posted. Every post is in the decision log (who posted what, when). A candidate's own withdrawal is visible to them immediately. The 90-day re-apply wait starts only from a rejection the candidate has been shown (or their withdrawal). Re-screening (one or all) is queued too, so HR isn't kept waiting either.

Candidate labels: Pending → Under review, Shortlisted, Interview stage, Offer extended, Hired, Not selected. Candidates never see scores.

**No AI?** With `LLM_PROVIDER=none` the app runs in "rules mode": template JD drafts and keyword-only screening.

## 3. Internal sharing, decision log and candidate ID

**Share with internal staff.** When approving a JD, HR chooses *Careers site + staff link* or *Staff link only*. The share page gives a private application link (copy it into Teams/WhatsApp/intranet, or email it from the app), a log of every email sent, a **Reset link** button and the posting choice. Applications through the link are tagged **Internal**. Without `APP_BASE_URL`, links use this PC's network address so colleagues on the same network can open them while the app runs (`python app.py` listens on the network; `HOST=127.0.0.1` keeps it local). `INTERNAL_EMAIL_DOMAINS` restricts recipients to staff addresses.

**Email.** `EMAIL_PROVIDER=smtp` (mailbox username + password; for Microsoft 365 IT must enable SMTP AUTH on that mailbox, and Microsoft switches basic-auth SMTP off by default for existing tenants at the end of December 2026, with re-enabling still possible) or `EMAIL_PROVIDER=graph` (Microsoft 365 via Microsoft Graph and OAuth: IT registers an app in Microsoft Entra ID with the **Mail.Send** application permission and gives you `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`; `GRAPH_SENDER` is the mailbox it sends from). On its own, the Mail.Send application permission lets the app send as *any* mailbox, so IT should limit it to the sending mailbox with [RBAC for Applications in Exchange Online](https://learn.microsoft.com/exchange/permissions-exo/application-rbac).

**External job sites.** On the share page, **Copy for job board** copies the JD as plain text with the public apply link, ready to paste into LinkedIn, Naukri, Indeed, Bayt and others. Public job pages also carry [Google for Jobs structured data](https://developers.google.com/search/docs/data-types/job-posting) (JobPosting JSON-LD); Google can only read it once the careers site is on a public web address. Closed jobs return "not found", which removes them.

**Decision log.** Every status change is recorded: submitted, auto-shortlisted (by the system, with score and threshold), moved forward or rejected by an HR user, withdrawn by the candidate. Rejections require a reason from a fixed list (or *Other* + text); any change can carry a note. **Decision log** in the HR menu filters by job, outcome (shortlisted / not shortlisted), who decided and dates, shows a *who decided what* summary, and exports CSV. Each candidate page shows its own timeline and the person's other applications.

**PAN / passport and the 90-day rule.** Candidates give a PAN or passport at sign-up (existing accounts are asked at their next login). The number is validated (PAN: 5 letters, 4 digits, 1 letter, 4th letter `P` for individuals; passport: 6–12 letters/digits plus issuing country) and **never stored in full**: only an HMAC hash (key `ID_HASH_KEY`) and the last 4 characters. One account per ID. After a rejection or withdrawal the person can apply again only after `REAPPLY_COOLDOWN_DAYS` (default 90), across any job or only the same title (`REAPPLY_SCOPE`). A new email address doesn't reset this because the ID is matched. Candidates see the date they can apply again.

**Existing databases upgrade themselves** on start (`schema_upgrade.py` adds the new columns and records existing statuses in the decision log; nothing is deleted).

## 4. AI layer: LangChain + Hugging Face, swappable to paid models

All AI calls go through LangChain, so the model is a configuration choice, not code:

| Mode | `.env` | Cost | Notes |
|---|---|---|---|
| **Hugging Face API** (default) | `LLM_PROVIDER=huggingface`<br>`LLM_MODEL=Qwen/Qwen2.5-7B-Instruct-1M`<br>`HUGGINGFACEHUB_API_TOKEN=hf_…` | Pay-as-you-go credits | Hosted open models through [HF Inference Providers](https://huggingface.co/docs/inference-providers/pricing). Since Oct 2026 free accounts get no monthly credits; PRO ($9/mo) includes $2/month. |
| **Hugging Face local** | `LLM_PROVIDER=huggingface_local`<br>`LLM_MODEL=Qwen/Qwen2.5-1.5B-Instruct` | Free | Runs on your machine via `transformers`. `pip install -r requirements-local.txt`. Slow on CPU (tens of seconds per JD); smaller model = weaker output. |
| **Paid** | `LLM_PROVIDER=openai` / `anthropic` / `google_genai` / `groq` …<br>`LLM_MODEL=…` + that provider's key | Per token | Uses LangChain's `init_chat_model`; install the matching `langchain-*` package. Gets native structured output automatically. |
| **Off** | `LLM_PROVIDER=none` | Free | Template JDs + keyword screening. |

- **Embeddings** (`EMBEDDINGS_PROVIDER`): `huggingface` (API, same token), `huggingface_local` (sentence-transformers, free), `openai`, or `none`. Default model `sentence-transformers/all-MiniLM-L6-v2`.
- **LangSmith tracing**: set `LANGSMITH_API_KEY` (or `LANGCHAIN_API_KEY`) and every chain run is traced: prompts, responses, latency, tokens, errors. Project name from `LANGSMITH_PROJECT` (default `talentflow`).
- **Check your setup**: `flask --app app ai-check` prints the configuration and makes one test call; the **HR team** page shows the same status.
- **Pick a live model**: `flask --app app ai-models qwen` lists chat models Hugging Face providers serve right now, with price per million tokens and JSON support (from `https://router.huggingface.co/v1/models`).
- **Failure handling**: if a call fails (no credits → 402, bad token → 401, rate limit → 429, model not served, unreadable JSON), the app falls back to the template JD or keyword score and shows HR a plain-English reason.

Code: `ai/providers.py` (model factory, the only provider-specific code), `ai/prompts.py` (ChatPromptTemplates), `ai/schemas.py` (Pydantic `ScreeningResult`), `ai/chains.py` (LCEL chains, JSON repair), `ai/__init__.py` (public API + fallbacks), `ai/templates.py` (rules-mode JD).

---

## Run locally (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-local.txt      &REM only for the free on-PC model
copy .env.example .env                       &REM then fill it in
flask --app app ai-check                     &REM tests the model, embeddings and tracing
start.bat                                    &REM or: python app.py
```

`python app.py` runs the production server (waitress) at http://127.0.0.1:5000 and prints the network address colleagues can use. Windows may ask to allow Python through the firewall: allow it on **Private networks**.

| Login | Email | Password |
|---|---|---|
| HR admin (auto-created) | admin@company.com | ChangeMe123! |
| Demo data (`flask --app app seed-demo`) | recruiter@company.com / priya@example.com … | Recruiter123! / Candidate123! |

Change the admin password under *Account* after first login. Add HR users in **HR team** or with `flask --app app create-hr`.

**Keys.** Leave `SECRET_KEY` and `ID_HASH_KEY` blank and the app creates random ones in `instance/` on first start (back up that folder). Or run `flask --app app gen-secrets` and paste the values into `.env`. Set `ID_HASH_KEY` once, before candidates register, and never change it.

**Branding.** `COMPANY_NAME`, `BRAND_COLOR`, `BRAND_ACCENT`, `COMPANY_WEBSITE`, `COMPANY_TAGLINE` in `.env`; put `logo.png` (header) and `logo-inverse.png` (dark footer) in `static/brand/`.

## Deploy

- **Docker:** `docker build -t talentflow . && docker run -p 8000:8000 --env-file .env -v $(pwd)/instance:/app/instance talentflow`
- **Render / Linux hosting:** uses the `Procfile` (gunicorn). Set `APP_BASE_URL` to the public address, use PostgreSQL via `DATABASE_URL` (uncomment `psycopg2-binary`), and a persistent disk for `instance/`.

## Project layout

```
app.py              app factory, CLI (seed-demo, create-hr, ai-check, ai-models, gen-secrets), server
config.py           settings from environment
models.py           User, Job, JDRevision, Application, ActivityLog (SQLAlchemy)
ai/                 LangChain AI layer: providers, prompts, schemas, chains, fallbacks
scoring.py          deterministic keyword/experience/similarity scorer
resume_parser.py    PDF/DOCX → text
requisition.py      requisition fields, validation, app-written JD sections, review checks
job_boards.py       Google for Jobs data, plain-text export for job sites
identity.py         PAN / passport validation and hashing
services.py         resume storage, screening, email (SMTP / Microsoft Graph), decision log
views_public.py     careers site
views_auth.py       login / register / account
views_candidate.py  apply, my applications, withdraw, resume download
views_hr.py         dashboard, requisitions, JD review/approval, pipeline, team
seed.py             demo data
templates/, static/ UI
```

## Security & compliance notes

- Passwords hashed (Werkzeug), CSRF on every form, role-based access (HR vs candidate), resumes stored under random names and only downloadable by HR or the owner, uploads limited to PDF/DOCX and 5 MB.
- AI output is rendered through a markdown sanitiser (nh3), never as raw HTML.
- Full audit trail: who created, edited, approved each JD; every status change per candidate.
- Automated screening of job applicants can be regulated (for example, [NYC Local Law 144](https://www.deloitte.com/us/en/services/audit/articles/nyc-local-law-144-algorithmic-bias.html) requires bias audits for automated employment decision tools). Keep a human reviewing every shortlist and check the rules that apply where you hire.

## Ideas for next steps

Candidate email notifications (applied / shortlisted / rejected), interview scheduling, SSO for HR (Microsoft Entra ID), background queue for large screening batches, bias reporting by stage, direct posting APIs for job sites.
