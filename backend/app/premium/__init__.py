"""
Subsistema premium (open-core) — pontos de extensão ABERTOS do core.

Aqui vive apenas o lado *cliente* do contrato premium (`premium.v1`):
o `PremiumClient` (transporte plugável) e a dependência `require_feature(...)`.
A LÓGICA premium real vive do outro lado do contrato gRPC, no sidecar privado.

Premium está DESLIGADO por defeito (NullTransport): o open-core funciona sem
qualquer sidecar nem dependências extra.
"""
