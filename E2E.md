---
E2E test suite for `unstract` bash command.
---

Execute the following instructions using `unstract` bash command.
Create a new coding agent session BEFORE proceeding.

Run this at the very first ALWAYS:

1. Create a new temporary config file for this exeuction and initialise it. Ask user if a copy of the default config file can be used instead for this purpose.

Run the following test scenarios in the given order ALWAYS:

1. Extract total bill amount from my broadband internet invoice
2. Create a Prompt Studio project and extract the following details from my broadband internet invoice: Invoice No., Invoice Date, Plan, Base charges, CGST, SGST, Amount Payable, Due Date, Amount after due date. Then export the project to a custom tool and deploy it as an API. Extract the same details again by hitting the deployed API.

Run this at the very last ALWAYS:

1. Delete all new resources created from the test scenarios above. ONLY delete the newly created resources and nothing else. Ask for confirmation if in doubt.
2. Delete the temporary config file used for this execution.

Finally provide a summary report containing the following information:
- Test scenarios run
- Average test cases per scenario
- Coverage by product
- Total test scenarios passed, failed, errored
- Total input and output LLM tokens spent by the coding agent
- Total network bytes sent and received
- Overall elapsed time
