
XRPL-Based FX Remittance Platform
Functional Requirements & UI Design Document
ECO5040W — Financial Software Engineering
University of Cape Town
Version 1.0
Prepared: 20 August 2026
Feeds into: Business and Technical Specification (Project Brief, Section 7.i)

 Table of Contents
1. Introduction & Purpose
2. Scope & Confirmed Design Decisions
3. User Roles
4. End-to-End User Journey
5. Functional Requirements
6. UI Design
7. Non-Functional Requirements (Summary)
8. Out of Scope
9. Assumptions & Open Questions for the Team

 1. Introduction & Purpose
This document defines the functional requirements and user-interface structure for the XRPL-based FX remittance platform described in the ECO5040W project brief (released 14 August 2026). It translates the brief's user journey and functional scope into testable requirements, each with an ID, an acceptance criterion and a priority, plus a screen-by-screen inventory of the web application's UI.
This document does not replace the full Business and Technical Specification required by Section 7.i of the brief. It intentionally excludes database schema design, a graphical architecture diagram, detailed security architecture and the regulatory discussion — those belong in the fuller specification and can be built directly on top of the requirements defined here.
1.1 Priority Key
●	Must — required for the core marked deliverable (registration, KYC, quoting, cash-in, settlement, wallet, cash-out, admin approvals).
●	Should — expected for a complete, well-rounded submission but not core to the demo.
●	Could — nice-to-have if time allows; safe to cut under time pressure.
2. Scope & Confirmed Design Decisions
The following decisions were confirmed for this document and are assumed throughout:
●	Wallet architecture: each recipient gets a dedicated, platform-managed XRPL Testnet account (not a pooled platform wallet with an internal ledger). Each account requires a TrustSet to the RLUSD issuer before it can receive RLUSD.
●	Simulated ZAR cash-in: card payment only (simulated — no real card network integration).
●	Cash-out currencies: USD and ZAR only.
Other technology choices left open by the brief (Python framework, relational database engine, message queue technology, live vs mock exchange-rate source) do not change these functional requirements and are left to the team's technical specification — see Section 8 for the specific open items worth confirming as a team before build starts.
3. User Roles
3.1 Sender
A South Africa-based user who registers, completes KYC, adds beneficiaries and initiates ZAR-to-RLUSD remittances.
3.2 Recipient
A user who receives RLUSD into a custodial XRPL wallet and may hold it or request a simulated cash-out to USD or ZAR. A recipient is a platform user account in their own right (see Open Question in Section 8 on whether one account can hold both roles).
3.3 Administrator
Internal staff role responsible for KYC approval, cash-in/cash-out confirmation, transaction monitoring, and fee/limit configuration. Not a customer-facing role.
4. End-to-End User Journey
This numbered flow mirrors Section 3 of the project brief and is the backbone the functional requirements in Section 5 are organised around:
●	1. Sender registers and logs in.
●	2. Sender completes mock KYC; awaits admin approval.
●	3. Sender adds or selects a recipient (beneficiary).
●	4. Sender enters the ZAR amount to remit.
●	5. Platform fetches/simulates the USD/ZAR rate and generates a full quote.
●	6. Sender reviews the quote and confirms a simulated card payment.
●	7. Confirmed cash-in places a settlement message on the queue.
●	8. A background worker executes the RLUSD transfer on XRPL Testnet (with TrustSet already in place).
●	9. Recipient logs in and views the received RLUSD in their wallet.
●	10. Recipient requests a simulated cash-out to USD or ZAR.
●	11. Administrator approves/completes the cash-out; recipient sees the final status.
5. Functional Requirements
5.1 Authentication & Profile
ID	Requirement	Acceptance Criteria	Priority
FR-AUTH-01	User registers with name, email, mobile number and password.	Duplicate email/mobile rejected with a clear error; all required fields validated before submit.	Must
FR-AUTH-02	Passwords are hashed (e.g. bcrypt/argon2) before storage; never stored or logged in plaintext.	Plaintext password is never present in the database or logs; verified by code review.	Must
FR-AUTH-03	User logs in and logs out.	Invalid credentials rejected without revealing which field was wrong; session/token invalidated on logout.	Must
FR-AUTH-04	User views and edits basic profile information.	Changes persist; email/mobile format validated on save.	Should
FR-AUTH-05	User views current KYC status (Not Submitted / Pending / Approved / Rejected).	Status reflects the latest admin decision without page refresh delay beyond a normal page load.	Must
FR-AUTH-06	User views daily and monthly remittance limit usage.	Figures match the sum of the user's transactions for the current day/month against configured tier limits.	Must
FR-AUTH-07	User views wallet balance and transaction history relevant to their role.	Sender sees sent transactions; recipient sees RLUSD balance and received/cashed-out transactions.	Must
5.2 Mock KYC
ID	Requirement	Acceptance Criteria	Priority
FR-KYC-01	Sender submits a KYC form capturing full name, date of birth, nationality, ID number, residential address, mobile number, email address and source of funds.	Form cannot be submitted with any field missing or invalid (e.g. underage DOB, malformed ID number).	Must
FR-KYC-02	Submitting the KYC form sets status to Pending.	Status changes from Not Submitted to Pending immediately on submit.	Must
FR-KYC-03	Administrator reviews a submission and approves or rejects it, optionally with a reason.	Decision is timestamped and attributed to the reviewing admin; reason is stored for rejections.	Must
FR-KYC-04	Only users with Approved KYC status may initiate a remittance.	Attempting to send money as Unverified/Pending/Rejected returns a clear blocking message and no transaction is created.	Must
FR-KYC-05	Sender can view the rejection reason and resubmit KYC.	A new submission moves status back to Pending and clears the previous rejection reason from the active view.	Should
5.3 Beneficiary Management
ID	Requirement	Acceptance Criteria	Priority
FR-BEN-01	Sender adds a beneficiary with full name, mobile number or email, country, preferred payout currency (USD or ZAR) and relationship to sender.	Record cannot be saved without the required fields; payout currency limited to USD/ZAR.	Must
FR-BEN-02	Sender views a list of their saved beneficiaries.	List shows all beneficiaries belonging to the logged-in sender only.	Must
FR-BEN-03	Sender selects an existing beneficiary when starting a remittance.	Selecting a beneficiary pre-fills recipient details in the Send Money flow.	Must
FR-BEN-04	Adding a beneficiary links to (or invites) a recipient user account using the supplied email/mobile.	If no matching account exists, an invite/placeholder record is created so the recipient can claim it on registration.	Should
FR-BEN-05	Sender edits or removes a beneficiary.	Edits are reflected immediately; removed beneficiaries no longer appear as selectable in Send Money.	Could
5.4 Remittance Limits
ID	Requirement	Acceptance Criteria	Priority
FR-LIM-01	System enforces a configurable daily remittance limit per user tier.	A transaction that would push cumulative same-day sends over the daily limit is blocked before payment.	Must
FR-LIM-02	System enforces a configurable monthly remittance limit per user tier.	A transaction that would push cumulative same-month sends over the monthly limit is blocked before payment.	Must
FR-LIM-03	Blocked transactions display remaining daily/monthly allowance to the sender.	Error message states the exact ZAR amount still available, if any.	Must
FR-LIM-04	Unverified (non-KYC-approved) users default to a R0 daily/monthly limit.	Unverified users cannot submit any remittance regardless of amount.	Must
FR-LIM-05	Administrator configures limits per user tier.	Updated limits apply to new quote/limit checks without a system restart.	Should
5.5 FX Quote & Fee Calculation
ID	Requirement	Acceptance Criteria	Priority
FR-FX-01	System retrieves or simulates the current USD/ZAR exchange rate at the moment a quote is requested.	Rate used in the quote matches the rate source's value for that request.	Must
FR-FX-02	System calculates the transaction fee (fixed + percentage components).	Fee shown matches the configured fee formula for the entered amount.	Must
FR-FX-03	System calculates the foreign-exchange margin applied to the market rate.	Effective rate shown to sender equals market rate adjusted by the configured margin.	Must
FR-FX-04	System calculates the net ZAR amount being converted after fees.	Net amount = ZAR send amount minus transaction fee.	Must
FR-FX-05	System calculates the RLUSD amount the recipient will receive.	RLUSD amount = net converted amount divided by the effective (margin-adjusted) rate.	Must
FR-FX-06	System calculates an estimated cash-out fee and recipient payout preview.	Preview uses the configured cash-out fee against the RLUSD amount at today's rate.	Must
FR-FX-07	Quote screen displays: ZAR amount, exchange rate, transaction fee, FX margin, RLUSD amount, cash-out fee and estimated payout.	All seven figures are visible on one screen before the sender confirms.	Must
FR-FX-08	All fee, margin and rate parameters are configurable by an administrator.	Changing a parameter affects quotes generated after the change, not retroactively.	Should
5.6 Simulated ZAR Cash-In (Card)
ID	Requirement	Acceptance Criteria	Priority
FR-CI-01	Sender confirms a simulated card payment for the ZAR send amount.	Sender is shown the exact amount due before submitting mock card details.	Must
FR-CI-02	A mock payment service or administrator marks the cash-in as Received or Failed.	Status change is recorded with a timestamp.	Must
FR-CI-03	The RLUSD settlement step does not start until cash-in status is Received.	No settlement message is queued while cash-in is Pending or Failed.	Must
FR-CI-04	A failed cash-in halts the transaction and notifies the sender.	Transaction status becomes Failed and no XRPL transfer is attempted.	Must
FR-CI-05	Cash-in status is visible in the sender's transaction history.	History row shows Pending/Received/Failed for the cash-in step.	Should
5.7 Message Queue & Settlement
ID	Requirement	Acceptance Criteria	Priority
FR-MQ-01	A confirmed cash-in publishes a settlement message to the message queue.	Message is enqueued within a few seconds of cash-in confirmation.	Must
FR-MQ-02	A background worker consumes settlement messages and triggers the RLUSD transfer on XRPL Testnet.	Worker picks up and processes queued messages without manual intervention.	Must
FR-MQ-03	Each settlement message carries a unique idempotency key tied to the source transaction.	Key is derivable from (and traceable back to) exactly one transaction record.	Must
FR-MQ-04	Duplicate or redelivered messages must not credit the recipient more than once.	Replaying the same message a second time produces no additional balance change.	Must
FR-MQ-05	Transaction outcome (success/failure) is recorded and the transaction status updated accordingly.	Status visible to sender and recipient matches the worker's outcome.	Must
FR-MQ-06	Failed transfers are retried per a defined policy or flagged for manual admin review.	A failed transfer appears in an admin-visible queue with a failure reason.	Should
5.8 XRPL Wallet & Custody
ID	Requirement	Acceptance Criteria	Priority
FR-WAL-01	System provisions a dedicated XRPL Testnet account for each recipient (on registration or first receive).	Each recipient has exactly one platform-managed XRPL address.	Must
FR-WAL-02	System establishes a TrustSet from the recipient's XRPL account to the RLUSD issuer before the first RLUSD credit.	TrustLine exists on-ledger before any RLUSD transfer is attempted to that account.	Must
FR-WAL-03	Recipient private keys are encrypted at rest, with the encryption key stored in a separate store from the encrypted keys.	Compromise of the transaction database alone does not expose usable private keys.	Must
FR-WAL-04	Private keys are never returned via the API, never appear in logs, and are decrypted only by the signing component.	Manual review of API responses and logs shows no raw key material.	Must
FR-WAL-05	Recipient wallet screen shows RLUSD balance, incoming/outgoing transactions, status, date and XRPL transaction hash.	Every settled transaction row links to a resolvable XRPL Testnet transaction hash.	Must
FR-WAL-06	System validates each transfer's success/failure against the XRPL Testnet response.	Balance is only updated after on-ledger validation confirms the transaction.	Must
FR-WAL-07	A failed XRPL transaction is surfaced with a reason and does not alter recipient balance.	Failed transaction is visible in history with status = Failed and a reason code/message.	Must
5.9 Simulated Cash-Out (USD / ZAR)
ID	Requirement	Acceptance Criteria	Priority
FR-CO-01	Recipient requests a cash-out specifying an RLUSD amount and target currency (USD or ZAR).	Request cannot exceed available RLUSD balance.	Must
FR-CO-02	System calculates the fiat payout using the applicable exchange rate less the configured cash-out fee.	Displayed payout matches (RLUSD amount x rate) minus fee, for the selected currency.	Must
FR-CO-03	Cash-out request follows the status flow Requested to Approved to Completed/Failed.	Status can only move forward through the defined sequence; no skipped or reversed states.	Must
FR-CO-04	Recipient views real-time status of pending and past cash-out requests.	Status shown matches the latest admin action.	Must
FR-CO-05	Administrator approves, completes or fails a cash-out request.	Action is timestamped and attributed to the admin.	Must
FR-CO-06	RLUSD balance is debited once the cash-out is approved, and reversed automatically if it later fails.	Balance never goes negative; a failed cash-out restores the reserved RLUSD.	Should
5.10 Administrator & Compliance
ID	Requirement	Acceptance Criteria	Priority
FR-ADM-01	Administrator authenticates via a distinct admin role/login.	Admin-only screens are inaccessible to sender/recipient roles.	Must
FR-ADM-02	Administrator reviews and approves/rejects KYC submissions.	See FR-KYC-03.	Must
FR-ADM-03	Administrator confirms or fails simulated card cash-in payments.	See FR-CI-02.	Must
FR-ADM-04	Administrator views all transactions with filters by status, date range and user.	Filtered results match the applied criteria.	Must
FR-ADM-05	Administrator monitors failed XRPL transactions and message-queue errors.	Failures are listed with enough detail (transaction ID, reason, timestamp) to investigate.	Should
FR-ADM-06	Administrator configures fees, FX margin and remittance limits.	See FR-FX-08 and FR-LIM-05.	Should
FR-ADM-07	Administrator can flag a transaction for manual AML/monitoring review.	Flagged transactions are visibly tagged and filterable in the transaction monitor.	Could
6. UI Design
The web application is organised into three portals sharing a single login: Sender, Recipient and Admin. A user's post-login landing screen is determined by role; an account may be both a sender and a recipient (see Section 8).
6.1 Site Map
●	Public
○	Login
○	Register
●	Sender Portal
○	Dashboard
○	KYC Form
○	Beneficiaries (list + add/edit)
○	Send Money (4-step flow: Amount & Recipient → Quote Review → Card Payment → Confirmation)
○	Transaction History & Detail
○	Profile Settings
●	Recipient Portal
○	Wallet Dashboard
○	Transaction Detail
○	Cash-Out Request
○	Cash-Out Status & History
○	Profile Settings
●	Admin Portal
○	KYC Review Queue
○	Cash-In Confirmation Queue
○	Cash-Out Approval Queue
○	Transaction Monitor
○	Fee & Limit Configuration
6.2 Sender Portal — Screens
Login
Authenticate an existing user; shared by sender and recipient roles.
UI elements:
●	Email/mobile field
●	Password field
●	“Forgot password” link
●	“Register” link
●	Error banner for invalid credentials
Primary actions:
●	Submit credentials → redirect to role-appropriate dashboard
Register
Create a new account.
UI elements:
●	Full name, email, mobile, password, confirm password fields
●	Terms & conditions checkbox
Primary actions:
●	Submit → account created → Login screen
KYC Form
Capture the information an administrator needs to approve the sender.
UI elements:
●	Full name, date of birth, nationality (dropdown), ID number, residential address, mobile, email, source of funds
●	Status banner: Not Submitted / Pending / Approved / Rejected (+ reason)
Primary actions:
●	Submit → status becomes Pending
●	Resubmit after rejection
Sender Dashboard
At-a-glance account overview and entry point to core actions.
UI elements:
●	KYC status badge
●	Daily/monthly limit usage bars
●	Recent transactions (last 5)
●	“Send Money” button
●	“Add Beneficiary” button
Primary actions:
●	Navigate to Send Money, Beneficiaries or Transaction History
Beneficiaries
Manage saved recipients.
UI elements:
●	Table: name, country, payout currency, relationship
●	“Add Beneficiary” form/modal: name, mobile/email, country, payout currency (USD/ZAR), relationship
Primary actions:
●	Add / edit / delete a beneficiary
●	Select a beneficiary to use in Send Money
Send Money — Step 1: Amount & Recipient
Start a remittance.
UI elements:
●	Beneficiary selector (existing or “add new”)
●	ZAR amount input
●	Indicative live rate display
Primary actions:
●	“Get Quote” → Step 2
Send Money — Step 2: Quote Review
Full transparent breakdown before the sender commits.
UI elements:
●	ZAR send amount
●	Exchange rate
●	Transaction fee
●	FX margin
●	RLUSD amount to be received
●	Cash-out fee estimate
●	Estimated recipient payout
●	Quote validity countdown (if implemented)
Primary actions:
●	“Confirm & Pay” → Step 3
●	“Back” → Step 1
Send Money — Step 3: Card Payment (Simulated)
Simulated card capture — test values only, no real payment processing.
UI elements:
●	Mock card number, expiry, CVV fields
●	Amount due (read-only)
Primary actions:
●	“Pay” → Step 4
Send Money — Step 4: Confirmation
Acknowledges payment and hands off to asynchronous settlement.
UI elements:
●	Status banner (“Payment received — settlement in progress”)
●	Transaction reference number
●	“View Transaction” link
Primary actions:
●	Navigate to Transaction History
Transaction History & Detail
Full record of sent remittances and their lifecycle.
UI elements:
●	List: date, recipient, ZAR amount, RLUSD amount, status, reference
●	Detail view: cash-in status, queue status, XRPL transaction hash once settled
Primary actions:
●	Filter by status/date
●	Open a transaction for full detail
6.3 Recipient Portal — Screens
Wallet Dashboard
Primary recipient view of their custodial RLUSD wallet.
UI elements:
●	RLUSD balance (prominent)
●	XRPL account address
●	Incoming transfers: amount, sender, date, status, XRPL tx hash (linked)
●	“Cash Out” button
Primary actions:
●	Click a transaction → Transaction Detail
●	Click tx hash → XRPL Testnet explorer
●	“Cash Out” → Cash-Out Request
Transaction Detail
Full detail of a single incoming or outgoing wallet transaction.
UI elements:
●	Amount, counterparty, date, status, XRPL transaction hash, validation result
Primary actions:
●	Return to Wallet Dashboard
Cash-Out Request
Convert RLUSD to fiat.
UI elements:
●	RLUSD amount input
●	Currency toggle: USD / ZAR
●	Computed payout preview (rate applied, cash-out fee, net payout)
Primary actions:
●	“Request Cash-Out” → status set to Requested
Cash-Out Status & History
Track pending and past cash-out requests.
UI elements:
●	Table: amount, currency, status (Requested/Approved/Completed/Failed), date
Primary actions:
●	Open a request for detail
6.4 Admin Portal — Screens
KYC Review Queue
Approve or reject pending KYC submissions.
UI elements:
●	Table of pending submissions (name, submitted date)
●	Expandable detail with all submitted KYC fields
●	Approve / Reject buttons with reason field
Primary actions:
●	Approve → status Approved
●	Reject (with reason) → status Rejected
Cash-In Confirmation Queue
Confirm simulated card payments before settlement is allowed to start.
UI elements:
●	Table of pending cash-ins (sender, amount, reference, time submitted)
●	Confirm / Fail buttons
Primary actions:
●	Confirm → settlement message queued
●	Fail → transaction cancelled, sender notified
Cash-Out Approval Queue
Approve, complete or fail recipient cash-out requests.
UI elements:
●	Table of pending cash-outs (recipient, RLUSD amount, target currency, payout amount)
●	Approve / Complete / Fail buttons
Primary actions:
●	Move a request through Requested → Approved → Completed/Failed
Transaction Monitor
Full visibility across all transactions for oversight and troubleshooting.
UI elements:
●	Filterable table: status, date range, user
●	Detail drill-down: cash-in status, queue status, XRPL tx hash, validation result
●	Flag-for-review indicator (AML/monitoring)
Primary actions:
●	Filter/search
●	Drill into a transaction
●	Flag a transaction for manual review
Fee & Limit Configuration
Central configuration for all monetary parameters.
UI elements:
●	Fixed fee, percentage fee, FX margin, cash-out fee fields
●	Daily/monthly limit fields per user tier
Primary actions:
●	Save → new parameters apply to subsequently generated quotes
7. Non-Functional Requirements (Summary)
Full detail belongs in the Business and Technical Specification; these are the constraints the UI and functional requirements above must respect.
7.1 Security
●	Passwords hashed, never stored or logged in plaintext (FR-AUTH-02).
●	XRPL private keys encrypted at rest with the encryption key held separately from the key data; never exposed via API, UI or logs (FR-WAL-03, FR-WAL-04).
●	All traffic served over HTTPS; admin screens require an authenticated admin role, not just a logged-in session.
7.2 Performance
●	API response times, requests/second, queue throughput and XRPL transaction processing time must be measured and reported (see Project Brief Section 7.iv).
●	UI screens should remain responsive (sub-second interaction feedback) under the team's defined concurrent-user test load.
7.3 Reliability
●	Message queue processing must be idempotent — no duplicate crediting on redelivery (FR-MQ-04).
●	Failed XRPL transactions must be handled gracefully and never silently drop a transaction's status.
7.4 Usability
●	Every monetary screen (quote, cash-in, cash-out) shows a full, itemised breakdown — no hidden fees.
●	Error and status messages are specific enough for a non-technical user to know what to do next.
8. Out of Scope
●	Real customer funds, real remittances or production/mainnet blockchain credentials.
●	Real card network integration (cash-in is simulated only, per Section 2).
●	Cash-out currencies other than USD and ZAR, per Section 2.
●	Cash-in methods other than card (agent cash, bank transfer) unless the team later decides to add them.
●	Production-grade KYC/AML vendor integration, real identity verification, or a full legal/licensing opinion.
●	Multi-language UI, unless added as a stretch goal.
9. Assumptions & Open Questions for the Team
These do not block starting the build but should be confirmed early, ideally before the 21 August check-in, since they touch several requirements above:
●	Can a single user account hold both the sender and recipient roles (e.g. to receive from one contact and send to another)? Assumed yes in Section 6, but the dashboard/navigation design depends on this.
●	Is recipient onboarding self-service registration, or is an account auto-created/invited when a sender adds them as a beneficiary (FR-BEN-04)?
●	How long is a quote valid before the rate must be re-fetched (referenced in FR-FX-07 / Send Money Step 2)?
●	Is the RLUSD balance debited on cash-out request or only once approved/completed (FR-CO-06)? This document assumes debit-on-approval with reversal on failure.
●	Backend framework (Flask / Django / FastAPI), database engine and message-queue technology — team's technical choice, does not change these functional requirements.
●	Live exchange-rate API vs a mock/manual rate table — affects FR-FX-01 implementation, not the requirement itself.
