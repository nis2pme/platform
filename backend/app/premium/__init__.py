"""
A fronteira entre o núcleo e o sidecar premium (`premium.v1`, gRPC com mTLS).

Aqui vive o lado do núcleo, e só ele: o que se pergunta ao sidecar, o que se
valida antes e o que se regista depois. O que o sidecar calcula e guarda vive do
outro lado do contrato.

  - Transporte e direitos: `client` (canal, prazos, reconexão e a cache de
    entitlements), `dependencies` (`require_feature`), `recusas` e `erros`
    (a tradução das respostas do sidecar em HTTP, uma só para todos os módulos).
  - Um módulo, três ficheiros: `<módulo>_client` (o contrato gRPC em dicionários),
    `<módulo>_router` (a rota fina: capacidade, identidades, auditoria) e, onde
    há regras do núcleo, o que lhe é próprio (`atores`, `contexto_nucleo`).
  - Ajudantes dos pedidos: `pedido` (cliente, idioma, o que um PATCH muda),
    `conversao` (o ator e os documentos), `cancela` (o que se aceita de um
    ficheiro antes de seguir para o sidecar).
  - Assistente de IA: `analise`, `context` e `sealing` (monta o contexto e sela-o
    com as chaves, que são do núcleo).
  - Licença e plano: `prazo_acesso` (avisos do fim do acesso) e `provisioning` (o
    plano de um tenant novo no gateway de entitlements).

O premium está desligado por omissão: o open-core funciona sem sidecar nem
dependências extra, e uma chamada que falha degrada só o módulo em causa.
"""
