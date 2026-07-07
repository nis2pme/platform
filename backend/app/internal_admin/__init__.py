"""
Gestão privilegiada de tenants (mecanismo, máquina-a-máquina).

Expõe operações destrutivas/sensíveis sobre uma empresa (suspender/reativar). É
**mecanismo**, não política: não sabe porque suspende — apenas executa ordens
autenticadas. A política de quando suspender vive fora do core.

Só é montado em DEPLOYMENT_MODE=saas e com o token configurado; em on-prem nem
existe. Ver app.main para o gate de montagem.
"""
