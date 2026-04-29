
curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/applications' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"application_id":"app:doc-ingestor","display_name":"Doc Ingestor"}' \
  | python -m json.tool
curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/users' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"user_id":"user:alice","display_name":"Alice"}' \
  | python -m json.tool
curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/principals' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"principal_id":"agent:doc-ingestor","kind":"agent","groups":["agent-dev"],"namespace":"tenant:kogwistar","application_id":"app:doc-ingestor","description":"Document summarizer"}' \
  | python -m json.tool
curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/quotas/upsert' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"lane":"principal","subject_id":"agent:doc-ingestor","quota_name":"hour","period":"hour","max_usd":10,"max_tokens":50000,"max_requests":500}' \
  | python -m json.tool
curl -fsS -X POST 'http://127.0.0.1:8789/admin/policy/quotas/upsert' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"lane":"user","subject_id":"user:alice","quota_name":"hour","period":"hour","max_usd":1,"max_tokens":20000,"max_requests":100}' \
  | python -m json.tool  
curl -fsS -X POST 'http://127.0.0.1:8789/admin/keys' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -F key_id='key:azure-openai:prod' \
  -F provider='azure_openai' \
  -F models='gpt-5.3-codex' \
  -F display_name='Azure OpenAI codex model production deployment' \
  -F upstream_url='https://opcd.openai.azure.com' \
  -F acl_mode='shared' \
  -F namespace='tenant:kogwistar' \
  -F shared_with_principals='agent:doc-ingestor' \
  -F provider_secret='3PtGXcQk5mTWFr3SSxWZRdvFXNzGIyNLCtvGgkIe03K0wXOePNdTJQQJ99CDACYeBjFXJ3w3AAABACOGcjKr' \
  | python -m json.tool
  