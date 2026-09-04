Cluster F (architecture/governance)
#360 CODE — 3 ordered PRs: PR1 derived AST rule tests/unit/test_governed_write_rule.py (roster-ratchet, hand-read floor, synthetic evasions, tokenizer cross-check) + narrow CLAUDE.md rule to what is true; PR2 core EvidenceIngestHandler(embed=soft|strict), worker handler = strict instantiation, route save_memory×2 / POST /documents / POST /evidence / ensure_evidence_document; PR3 = file issue on metadata-only writers. Findings: ~18 doc put/delete + 6 vector sites outside stores/+mutate/; NO governed document delete exists; redaction.apply entity-only. Owner already chose option B (ledger T-3). Risks: double events on save_memory vs #461 banner roster; execute_mutation gains agent-reachable evidence.ingest.
#256 CODE(seam) / OWNER(removal) — entry-point resolution ALREADY shipped (plugins/loader.py). Real coupling = registry._instantiate importing bolt modules. Plan: prepare_registry_params class hook + RegistryContext; synthetic plugin test; AST rule registry.py imports no bolt module (fails today: 3 imports, 4 name comparisons). Do not move files/extras. Owner Q: separate dist vs in-tree — recommend in-tree.
#257 OWNER — ADR does not exist; roadmap §G.4 still scopes PDF/transcript handlers. Q: confirm strike them. Recommend yes → docs S.
#194 OWNER — drop #256 dependency (collect-seam gate touches zero backends). Recommend wait for partner signal; remove sequencing claim from backlog.
#474 OWNER lean DEFER — require_approval is hard deny not pending; shape would be doc-plane `unconfirmed` stamp + gate + confirm. Recommend not built until incident measured.
#475 OWNER — Q: approve first line set (newest-evidence age; graph recency-window share)? Recommend yes → S CODE.
#476 OWNER — no space axis exists. Q: what stamps the space? Recommend hook-derived repo name, default-pass.
#477 OWNER — Q: accept citation-as-verification for last_verified? Recommend yes.
#478 DEFER umbrella.
Dispatch: 360-PR1 ∥ 256-PR1; then 360-PR2.
