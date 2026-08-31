# ParaBank JMeter Test — Fix Log & Setup Guide

## Environment
- JMeter 5.6.3, Windows 10
- ParaBank: `localhost:8080`
- HSQLDB: `localhost:9001` (driver `org.hsqldb.jdbcDriver`, url `jdbc:hsqldb:hsql://localhost:9001/parabank`, user `sa`)
- 100 users: `testuser001`–`testuser100`, password `Password123`
- Accounts: 61001–61100 (main), 62001–62100 (new)
- Modules: Login, Overview, OpenAccount, Transfer, BillPay, Loan, UpdateProfile, FindTransactions, Logout

## Files (final versions)
| File | Purpose |
|---|---|
| `ParaBank_AllModules_REPORT_FIXED3_DB.jmx` | Full test plan — HTTP modules + JDBC DB verification samplers |
| `generate_pdf_reports_v2.py` | Builds `Performance_Report.pdf` + `Database_Report.pdf` from `results.jtl` |
| `parabank_100_users.csv` | Test data (username, password, customer_id, account_id, to_account_id, account_type, module) |

---

## Bug 1 — HTML report failed: column mismatch
**Error:**
```
Mismatch between expected number of columns:16 and columns in CSV file:17
```
**Cause:** Two listeners writing to the same `results.jtl` at once — a `Summary Report` listener inside the JMX had a hardcoded `<stringProp name="filename">results.jtl</stringProp>`, which collided with the `-l` path passed on the command line. Two different `SampleSaveConfiguration`s interleaving into one file caused inconsistent column counts mid-file.

**Fix:** Cleared the hardcoded filename in the listener's `ResultCollector`:
```xml
<stringProp name="filename"></stringProp>
```
Now only the command-line `-l` path controls output. Single writer, consistent columns.

---

## Bug 2 — Loan (and other) module missing from report entirely
**Symptom:** HTML report only showed `Login`, `Overview`, `OpenAccount`, `FindTransactions`, `Logout` — zero requests for `Transfer`, `BillPay`, `Loan`, `UpdateProfile`.

**Cause:** CSV Data Set Config had:
```xml
<stringProp name="shareMode">shareMode.thread</stringProp>
```
`shareMode.thread` gives each thread its own **private** row pointer starting at row 1. Since each thread only loops once, **every one of the 100 threads read row 1 of the CSV** — same `module` value for all threads. Rows 66–85 (`LOAN`) and other module rows were never consumed by any thread.

**Fix:** Changed to:
```xml
<stringProp name="shareMode">shareMode.all</stringProp>
```
Now the file is shared across all threads sequentially — thread 1 → row 1, thread 2 → row 2, ... thread 100 → row 100. All modules now get exercised proportional to their rows in the CSV.

---

## Bug 3 — Wrong/leftover run command
Running the `.jmx` path directly (without `jmeter.bat -n -t`) does nothing — Windows tries to "execute" the XML file itself and silently fails with no error. Correct command always needs the full JMeter invocation:
```bat
"C:\apache-jmeter-5.6.3\bin\jmeter.bat" -n -t "C:\parabank\jmeter+csv\ParaBank_AllModules_REPORT_FIXED3_DB.jmx" -l "C:\parabank\jmeter+csv\results.jtl" -e -o "C:\parabank\jmeter+csv\html_report"
```

---

## Addition 1 — JDBC (DB) verification samplers
Added a `JDBC Connection Configuration` element plus 5 `JDBC Request` samplers (one per module, run right after its HTTP step), all prefixed `DB -` so the PDF script recognizes them:

| Sampler | Query (complex — join/aggregate) |
|---|---|
| `DB - Verify New Account` | `SELECT A.ID, A.TYPE, A.BALANCE, C.FIRST_NAME, C.LAST_NAME FROM ACCOUNT A JOIN CUSTOMER C ON A.CUSTOMER_ID = C.ID WHERE A.CUSTOMER_ID = ? ORDER BY A.ID DESC;` |
| `DB - Verify Transfer` | `SELECT T.ID, T.ACCOUNT_ID, T.TYPE, T.DATE, T.AMOUNT, T.DESCRIPTION, A.CUSTOMER_ID FROM TRANSACTION T JOIN ACCOUNT A ON T.ACCOUNT_ID = A.ID WHERE T.ACCOUNT_ID IN (?, ?) ORDER BY T.ID DESC;` |
| `DB - Verify Bill Pay` | `SELECT T.ID, T.ACCOUNT_ID, T.AMOUNT, T.DESCRIPTION, A.BALANCE FROM TRANSACTION T JOIN ACCOUNT A ON T.ACCOUNT_ID = A.ID WHERE T.ACCOUNT_ID = ? AND T.TYPE = 1 ORDER BY T.ID DESC;` |
| `DB - Verify Loan Account` | `SELECT A.CUSTOMER_ID, COUNT(*) AS LOAN_COUNT, SUM(A.BALANCE) AS TOTAL_BALANCE FROM ACCOUNT A WHERE A.CUSTOMER_ID = ? AND A.TYPE = 2 GROUP BY A.CUSTOMER_ID;` |
| `DB - Verify Profile Update` | `SELECT C.ID, C.FIRST_NAME, C.LAST_NAME, C.ADDRESS, C.CITY, C.STATE, C.ZIP_CODE, C.PHONE_NUMBER, COUNT(A.ID) AS ACCOUNT_COUNT FROM CUSTOMER C LEFT JOIN ACCOUNT A ON A.CUSTOMER_ID = C.ID WHERE C.ID = ? GROUP BY C.ID, ...;` |

**Prerequisite:** HSQLDB driver jar must exist in `C:\apache-jmeter-5.6.3\lib\` (else `ClassNotFoundException` on run).

## Addition 2 — Exception Timestamp Log (PDF script)
Added `exception_time_log(df)` function to `generate_pdf_reports.py`. Unlike the existing grouped "Failure Detail" table (one row per endpoint+code+reason, first-seen time only), this lists **every individual failed query occurrence** with its exact `YYYY-MM-DD HH:MM:SS.mmm` timestamp, module, HTTP code, reason, and response time. Rendered as a new section in `Database_Report.pdf`: **"Exception Timestamp Log (every failed query, exact time)"**.

---

## Full run sequence (from scratch)
```bat
cd C:\parabank\jmeter+csv

del "C:\parabank\jmeter+csv\results.jtl"
rmdir /s /q "C:\parabank\jmeter+csv\html_report"

"C:\apache-jmeter-5.6.3\bin\jmeter.bat" -n -t "C:\parabank\jmeter+csv\ParaBank_AllModules_REPORT_FIXED3_DB.jmx" -l "C:\parabank\jmeter+csv\results.jtl" -e -o "C:\parabank\jmeter+csv\html_report"

start "" "C:\parabank\jmeter+csv\html_report\index.html"

python generate_pdf_reports_v2.py "C:\parabank\jmeter+csv\results.jtl"
```
Outputs: `html_report\index.html`, `Performance_Report.pdf`, `Database_Report.pdf` (all in `C:\parabank\jmeter+csv\`).

## One-time setup
```bat
pip install pandas matplotlib reportlab
```
