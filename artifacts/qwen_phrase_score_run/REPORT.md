# Direct skill-phrase completion findings

This analysis complements and does not replace the binary Yes/No elicitation in
`artifacts/qwen_score_run`. Each canonical skill phrase is teacher-forced in full
after three completion prompts. Raw logits remain token-level provenance and are
never compared across the checkpoints' different vocabularies.

The reported probabilities are normalized within each model and prompt over the
14 approved taxonomy phrases. They describe model-relative phrase completion, not
historical hotel work, worker capabilities, vacancies, or labour demand.

## Length-normalized within-model ranks

| model | rank | skill | mean normalized probability | prompt range |
| --- | ---: | --- | ---: | ---: |
| qwen1_5_0_5b_chat_2024 | 1 | Attention to detail | 0.53507919 | 0.03846201 |
| qwen1_5_0_5b_chat_2024 | 2 | Multitasking | 0.20615230 | 0.14664088 |
| qwen1_5_0_5b_chat_2024 | 3 | Customer service | 0.17473018 | 0.12442122 |
| qwen1_5_0_5b_chat_2024 | 4 | Time management | 0.02868481 | 0.03306922 |
| qwen1_5_0_5b_chat_2024 | 5 | Payment processing | 0.01917876 | 0.00541454 |
| qwen1_5_0_5b_chat_2024 | 6 | Teamwork | 0.01496037 | 0.01655457 |
| qwen1_5_0_5b_chat_2024 | 7 | Problem solving | 0.00561954 | 0.00911455 |
| qwen1_5_0_5b_chat_2024 | 8 | Complaint handling | 0.00445066 | 0.01082122 |
| qwen1_5_0_5b_chat_2024 | 9 | Local knowledge | 0.00389765 | 0.00375490 |
| qwen1_5_0_5b_chat_2024 | 10 | Record keeping | 0.00384273 | 0.00640300 |
| qwen1_5_0_5b_chat_2024 | 11 | Guest communication | 0.00300262 | 0.00423728 |
| qwen1_5_0_5b_chat_2024 | 12 | Reservation systems | 0.00029427 | 0.00023700 |
| qwen1_5_0_5b_chat_2024 | 13 | AI literacy | 0.00007313 | 0.00016581 |
| qwen1_5_0_5b_chat_2024 | 14 | Data privacy | 0.00003380 | 0.00002931 |
| qwen3_5_0_8b_2026 | 1 | Attention to detail | 0.58967345 | 0.18043232 |
| qwen3_5_0_8b_2026 | 2 | Multitasking | 0.20113027 | 0.07984890 |
| qwen3_5_0_8b_2026 | 3 | Customer service | 0.11682850 | 0.02502099 |
| qwen3_5_0_8b_2026 | 4 | Guest communication | 0.02934261 | 0.06235805 |
| qwen3_5_0_8b_2026 | 5 | Time management | 0.02066037 | 0.00734318 |
| qwen3_5_0_8b_2026 | 6 | Teamwork | 0.01878801 | 0.00417846 |
| qwen3_5_0_8b_2026 | 7 | Complaint handling | 0.00788472 | 0.01368976 |
| qwen3_5_0_8b_2026 | 8 | Payment processing | 0.00446757 | 0.00262627 |
| qwen3_5_0_8b_2026 | 9 | Problem solving | 0.00403933 | 0.00359145 |
| qwen3_5_0_8b_2026 | 10 | Local knowledge | 0.00351579 | 0.00308575 |
| qwen3_5_0_8b_2026 | 11 | Record keeping | 0.00184302 | 0.00524915 |
| qwen3_5_0_8b_2026 | 12 | Reservation systems | 0.00122390 | 0.00084655 |
| qwen3_5_0_8b_2026 | 13 | Data privacy | 0.00047186 | 0.00036397 |
| qwen3_5_0_8b_2026 | 14 | AI literacy | 0.00013058 | 0.00015975 |

Sequence-total and length-normalized rankings are both retained because
phrase token counts differ. Prompt ranges and descriptive prompt-resampling
intervals use only three fixed phrasings and are not population confidence intervals.
