"""
PremiumClient — cliente fino (no core, ABERTO) para o contrato premium.v1.

O core fala SEMPRE com o sidecar premium através deste cliente. O transporte é
plugável e escolhido por configuração:

  - NullTransport  (default): premium DESLIGADO. Toda a feature = não-autorizada.
                   Mantém o open-core funcional sem qualquer sidecar a correr.
  - GrpcTransport  (PREMIUM_ENABLED=true): liga ao sidecar via gRPC (premium.v1).

A AUTORIDADE dos direitos é sempre do sidecar; e o sidecar é DONO do ciclo de vida
do job de análise IA. Este cliente apenas pergunta o entitlement (com cache curta),
submete o contexto e lê o estado do job.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from functools import lru_cache

from app.config import get_settings
from app.premium.schemas import Entitlement, EstadoLicenca, LicencaInstalada

logger = logging.getLogger(__name__)

# Janela do rate-limit do gateway → sufixo curto que o frontend traduz (janela.*).
_JANELA_CURTA = {"req_hora": "hora", "req_dia": "dia", "req_mes": "mes"}


class PremiumIndisponivelError(RuntimeError):
    """O sidecar não está utilizável: transporte por montar, canal em baixo.

    Existe para distinguir duas coisas que antes chegavam ao cliente iguais. Os
    routers já respondiam `503 premium_indisponivel` quando o cliente nem sequer
    era construído, mas uma falha ao **montar o canal** — sidecar a reiniciar,
    licença por ativar, material de TLS em falta — levantava `RuntimeError`, que
    não é um erro de gRPC e escapava do tradutor de erros para o handler
    genérico. O resultado era `500 Erro interno do servidor` em todas as rotas
    premium: quem via não conseguia distinguir «o teu módulo está indisponível»
    de «a aplicação tem um defeito».

    Ao ser um tipo próprio, um `TypeError` ou um `KeyError` nossos continuam a
    sair como 500 — que é o que devem ser. Só a indisponibilidade vira 503.
    """


# Códigos de gRPC que significam «não se chegou ao sidecar», por oposição a «o
# sidecar respondeu e disse que não». Ficam aqui, num sítio só, porque a lista é
# a mesma para o gate de features e para os cinco tradutores de erro dos routers
# — e uma cópia que envelhecesse à parte das outras é como nasce a incoerência
# que isto veio corrigir.
_NOMES_INDISPONIVEL = ("UNAVAILABLE", "DEADLINE_EXCEEDED")

# Prazo de omissão das leituras ao sidecar (as de `_PREFIXOS_LEITURA`). Sem
# prazo, com o sidecar parado, uma leitura levava 4–12 s a falhar (ligação que
# falha devagar + uma repetição depois de remontar o canal) — e o
# `CheckEntitlement` está à frente de todas as rotas premium, e o cartão da
# licença existe precisamente para dizer «indisponível» nesse momento.
# Medido: as leituras mais pesadas respondem em menos de 0,1 s (listagens
# paginadas), por isso 3 s dá folga larga sem deixar o ecrã pendurado. Quem
# precisa de outro prazo passa `timeout=` e esse prevalece. As escritas e as
# operações longas (importação, backup) não levam prazo de omissão; a análise
# leva o seu (GrpcTransport.PRAZO_ANALISE_S).
PRAZO_LEITURA_S = 3.0


def e_valor_fora_do_contrato(exc: BaseException) -> bool:
    """True se um número do pedido não cabe no tipo do contrato (int32/int64).

    O protobuf lança `ValueError` ao montar a mensagem, antes de haver chamada
    ao sidecar. É um pedido inválido (400), não uma avaria da plataforma (500).
    """
    return isinstance(exc, ValueError) and "out of range" in str(exc)


def e_indisponibilidade(exc: BaseException) -> bool:
    """True se a exceção significa que o sidecar não está a ser alcançado.

    `UNAVAILABLE` é o sidecar em baixo, a reiniciar ou inacessível na rede.
    `DEADLINE_EXCEEDED` é o sidecar pendurado — do ponto de vista de quem chama
    dá no mesmo, e a resposta certa é a mesma.

    Fica de fora tudo o que seja o sidecar a responder: um `INVALID_ARGUMENT` ou
    um `PERMISSION_DENIED` são conversas bem sucedidas com um desfecho negativo,
    e traduzi-los para 503 diria ao cliente para tentar mais tarde uma coisa que
    nunca vai passar.
    """
    if isinstance(exc, PremiumIndisponivelError):
        return True
    try:
        import grpc  # type: ignore
    except ImportError:
        return False
    if not isinstance(exc, grpc.RpcError):
        return False
    codigo = exc.code() if callable(getattr(exc, "code", None)) else None
    return getattr(codigo, "name", "") in _NOMES_INDISPONIVEL


class AnaliseLimiteError(RuntimeError):
    """Limite atingido (rate-limit por janela/metering do gateway, ou já-em-curso).

    Carrega o `detalhe` já pronto para o corpo do 402/429 (o frontend traduz por
    i18n a partir de `detalhe["codigo"]`).
    """

    def __init__(self, detalhe: dict) -> None:
        super().__init__(detalhe.get("codigo", "limite"))
        self.detalhe = detalhe


def _detalhe_429(details: str) -> dict:
    """Mapeia o corpo de um RESOURCE_EXHAUSTED do sidecar para o detalhe do 429.

    Aceita:
      - `{"detail": {"codigo": ...}}`  → já final (ex.: "limite_em_curso"); passa.
      - `{"detail": {"janela"/"reason"/"limite"/"reset_em"}}` → rate-limit do gateway.
      - `{"detail": "<texto>"}` ou parse falhado → limite genérico.
    """
    try:
        corpo = json.loads(details)
    except (ValueError, TypeError):
        return {"codigo": "limite"}

    det = corpo.get("detail") if isinstance(corpo, dict) else corpo
    if isinstance(det, dict):
        if "codigo" in det:
            return det
        janela = det.get("janela")
        if janela:
            return {
                "codigo": "limite_janela",
                "janela": _JANELA_CURTA.get(janela, janela),
                "limite": det.get("limite"),
                "reset_em": det.get("reset_em"),
            }
        if det.get("reason") == "limit_reached":
            return {"codigo": "limite_tokens"}
    return {"codigo": "limite"}


def criar_canal_sidecar(grpc, addr):
    """Cria o canal gRPC para o sidecar premium — mTLS com pinning da CA a partir de
    PREMIUM_TLS_CA/CLIENT_CERT/CLIENT_KEY.

    Partilhado por todos os clientes do core que falam com o sidecar (análise IA,
    inventário, ...). O core apresenta o seu cert de cliente e verifica o do sidecar
    contra a CA dada; `PREMIUM_TLS_SERVER_NAME` alinha o nome verificado (SAN).

    FAIL-CLOSED: sem material mTLS o canal NÃO é criado. Tudo o que atravessa este
    canal — inventário, risco, fornecedores, pesquisa, backup — são dados do cliente,
    e um canal em claro exporia-os a quem partilhe a rede dos contentores. O escape
    PREMIUM_DEV_SEM_MTLS=1 existe para dev local e deixa rasto no log."""
    import os

    ca = os.getenv("PREMIUM_TLS_CA")
    cert = os.getenv("PREMIUM_TLS_CLIENT_CERT")
    key = os.getenv("PREMIUM_TLS_CLIENT_KEY")
    if ca and cert and key:
        from pathlib import Path

        creds = grpc.ssl_channel_credentials(
            root_certificates=Path(ca).read_bytes(),
            private_key=Path(key).read_bytes(),
            certificate_chain=Path(cert).read_bytes(),
        )
        options = []
        server_name = os.getenv("PREMIUM_TLS_SERVER_NAME")
        if server_name:
            options.append(("grpc.ssl_target_name_override", server_name))
        return grpc.secure_channel(addr, creds, options=options)

    if os.getenv("PREMIUM_DEV_SEM_MTLS") == "1":
        logger.warning(
            "[SEGURANCA] canal para o sidecar SEM mTLS (PREMIUM_DEV_SEM_MTLS=1) — "
            "os dados viajam em claro. Apenas para desenvolvimento local."
        )
        return grpc.insecure_channel(addr)

    em_falta = [
        nome
        for nome, valor in (
            ("PREMIUM_TLS_CA", ca),
            ("PREMIUM_TLS_CLIENT_CERT", cert),
            ("PREMIUM_TLS_CLIENT_KEY", key),
        )
        if not valor
    ]
    raise PremiumIndisponivelError(
        "canal para o sidecar sem mTLS (fail-closed): em falta "
        f"{', '.join(em_falta)}. Definir estas variáveis (os certificados são gerados "
        "automaticamente pelo serviço premium-certs), ou PREMIUM_DEV_SEM_MTLS=1 em dev."
    )


# Estado do job (enum gerado: PENDENTE=0, PROCESSANDO=1, CONCLUIDO=2, ERRO=3).
_ESTADO_MAP = {0: "pendente", 1: "processando", 2: "concluido", 3: "erro"}


def _job_pb_to_dict(pb) -> dict | None:
    """Converte o AnaliseJob (protobuf) num dict simples. `job_id` vazio = sem job."""
    if not pb.job_id:
        return None
    estado = _ESTADO_MAP.get(pb.estado, "erro")
    relatorio = None
    if pb.estado == 2:  # CONCLUIDO
        r = pb.relatorio
        relatorio = {
            "resumo_executivo": r.resumo_executivo,
            "pontos_positivos": list(r.pontos_positivos),
            "lacunas_identificadas": list(r.lacunas_identificadas),
            "recomendacoes": list(r.recomendacoes),
            "score_qualidade_documentacao": r.score_qualidade_documentacao,
            "score_robustez_implementacao": r.score_robustez_implementacao,
            "nivel_confianca": r.nivel_confianca,
            "gerado_em": r.gerado_em,
        }
    return {
        "job_id": pb.job_id,
        "controlo_empresa_id": pb.controlo_empresa_id,
        "estado": estado,
        "relatorio": relatorio,
        "erro_codigo": pb.erro_codigo or None,
        "erro_categoria": pb.erro_categoria or None,
        "created_at": pb.created_at,
        "updated_at": pb.updated_at,
        "auditoria_pendente": pb.auditoria_pendente,
        "pedido_por": pb.pedido_por or None,
    }


class PremiumTransport:
    """Interface de transporte do contrato premium.v1."""

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        raise NotImplementedError

    def estado_licenca(self, tenant_id: str, nif: str = "") -> EstadoLicenca:
        """Estado agregado da licença do tenant (para o cartão no UI). O `nif` e o
        `tenant_id` (a empresa) entram no código de instalação que o sidecar compõe."""
        raise NotImplementedError

    def instalar_licenca(
        self, envelope_json: str, nif: str, so_validar: bool, tenant_id: str
    ) -> LicencaInstalada:
        """Instala (ou só valida) um ficheiro de licença no sidecar (on-prem). A
        licença tem de ser da empresa `tenant_id` e do NIF dela."""
        raise NotImplementedError

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        """Submete uma análise IA (client-streaming) e devolve o job (dict)."""
        raise NotImplementedError

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool
    ) -> dict | None:
        """Estado/resultado do job mais recente de um controlo (ou None se não há)."""
        raise NotImplementedError


class NullTransport(PremiumTransport):
    """Premium desligado — tudo não-autorizado. É o default do open-core."""

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        return Entitlement.disabled(feature, reason="premium_disabled")

    def estado_licenca(self, tenant_id: str, nif: str = "") -> EstadoLicenca:
        return EstadoLicenca.simples("sem_premium")

    def instalar_licenca(
        self, envelope_json: str, nif: str, so_validar: bool, tenant_id: str
    ) -> LicencaInstalada:
        return LicencaInstalada(aceite=False, codigo_erro="nao_suportado")

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        raise RuntimeError("Premium desligado — análise IA indisponível.")

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool
    ) -> dict | None:
        raise RuntimeError("Premium desligado — análise IA indisponível.")


class _StubComDescarte:
    """Envolve um stub gerado: cada RPC passa pelo caminho que larga o canal.

    Existe para que **nenhuma chamada possa esquecer-se** de o fazer. A versão
    anterior punha a reconexão num método (`_invocar`) que cada chamada tinha de
    lembrar-se de usar — e os clientes de módulo, que servem as rotas premium,
    nunca o usaram: chamavam o RPC direto no stub. O resultado é que o canal
    deles nunca era largado quando o sidecar desaparecia, e ficava preso no recuo
    progressivo do gRPC até alguém reiniciar o núcleo.

    Devolver o stub já embrulhado inverte o defeito: passa a ser preciso trabalho
    deliberado para escapar à reconexão, em vez de trabalho deliberado para a ter.
    """

    __slots__ = ("_stub", "_dono")

    def __init__(self, stub, dono) -> None:
        self._stub = stub
        self._dono = dono

    def __getattr__(self, nome):
        chamada = getattr(self._stub, nome)

        def _chamar(*args, **kwargs):
            return self._dono._invocar(chamada, *args, _nome_rpc=nome, **kwargs)

        return _chamar


# Prefixos dos RPC que só leem. Só estes se repetem sozinhos depois de o canal ser
# remontado: um `UNAVAILABLE` pode chegar depois de o sidecar já ter feito a
# escrita (a ligação cai a meio da resposta), e repetir uma escrita duplicava-a.
# Lista fechada e não «tudo menos as escritas»: um RPC novo nasce sem repetição
# até alguém decidir que é seguro.
_PREFIXOS_LEITURA = (
    "Listar", "Obter", "Check", "Estado", "Catalogo", "Constatacoes", "Detalhe",
    "Factos", "Historico", "Painel", "Pesquisar", "Qualidade",
)


def _e_leitura(nome_rpc: str) -> bool:
    return nome_rpc.startswith(_PREFIXOS_LEITURA)


def _canal_morto(exc: BaseException) -> bool:
    """O canal não alcança o sidecar (`UNAVAILABLE`, ou transporte por montar).
    Só isto justifica largá-lo; um prazo esgotado não."""
    if isinstance(exc, PremiumIndisponivelError):
        return True
    codigo = exc.code() if callable(getattr(exc, "code", None)) else None
    return getattr(codigo, "name", "") == "UNAVAILABLE"


def _repetivel(nome_rpc: str, exc: BaseException) -> bool:
    """Uma leitura que falhou por o sidecar não ser alcançado neste canal.

    Só `UNAVAILABLE`: é o canal velho a bater numa ligação que já não existe (o
    sidecar reiniciou). `DEADLINE_EXCEEDED` fica de fora — já se esperou o prazo
    inteiro, e repetir dobrava a espera de quem está à frente do ecrã.
    """
    if not _e_leitura(nome_rpc):
        return False
    try:
        import grpc  # type: ignore
    except ImportError:
        return False
    if not isinstance(exc, grpc.RpcError):
        return False
    codigo = exc.code() if callable(getattr(exc, "code", None)) else None
    return getattr(codigo, "name", "") == "UNAVAILABLE"


class ClienteSidecar:
    """Ciclo de vida do canal gRPC para o sidecar: montar, largar, remontar.

    Base comum do transporte de entitlements e dos clientes de cada módulo. O que
    aqui vive não é detalhe de transporte: é a diferença entre uma manutenção de
    30 s no sidecar e uma interrupção do produto até alguém reiniciar o núcleo.

    Uma subclasse só precisa de declarar o nome do seu stub em `_NOME_STUB`.
    """

    # Nome da classe de stub em `premium_pb2_grpc` (ex.: "RiscoServiceStub").
    _NOME_STUB: str = ""

    # Tempo mínimo entre duas reconstruções do canal. Sem travão nenhum, uma
    # paragem longa faz cada pedido montar um canal novo — troca-se um canal
    # preso por uma tempestade de ligações, que é pior.
    #
    # **Tem de ser bem menor do que qualquer cadência realista de repetição**, e
    # isto custou a perceber: com 5,0 s, um cliente que repetisse de 5 em 5 s
    # chegava aqui sempre um pouco antes de a janela fechar, o travão recusava o
    # descarte *todas* as vezes, e o canal condenado nunca era substituído —
    # ficava preso para sempre. O sintoma era intermitente (dependia de o
    # instante cair de um lado ou do outro da janela), que é a pior forma de um
    # defeito aparecer: mede-se duas vezes e dá duas respostas.
    #
    # A protecção contra várias reconstruções em paralelo não vem daqui, vem do
    # `_lock` do `_ensure_stub`: só uma thread monta o canal, as outras esperam e
    # reutilizam-no. Este travão limita só o *ritmo*, e para isso um segundo
    # chega — deixa passar qualquer repetição a partir de 1 s.
    _INTERVALO_RECONEXAO = 1.0
    # Folga antes de fechar um canal largado: maior do que a operação mais longa
    # que pode estar a correr nele (a aplicação de uma importação, 180 s).
    _GRACA_FECHO_S = 300.0

    def __init__(self, addr: str) -> None:
        self._addr = addr
        self._lock = threading.Lock()
        self._stub = None
        self._canal = None
        # `-inf` e não `0.0`: o primeiro descarte tem de passar sempre, sem
        # depender do valor absoluto do relógio monotónico.
        self._ultimo_descarte = float("-inf")
        # Contadores do ciclo de vida do canal. Existem porque, sem eles, um
        # canal que não recupera é indistinguível de um sidecar em baixo: as
        # duas coisas dão 503 e nenhuma deixa rasto. Quem diagnostica precisa de
        # saber se o canal chegou a ser reconstruído, quantas vezes, e quantos
        # descartes o travão de ritmo recusou.
        self._canais_montados = 0
        self._descartes = 0
        self._descartes_travados = 0

    def _ensure_stub(self):
        # O caminho rápido guarda o valor numa variável local em vez de o voltar
        # a ler no fim: entre a verificação e o `return`, outra thread pode estar
        # a largar o canal, e devolver `None` daqui dava um `AttributeError` —
        # que, por não ser falha de transporte, sairia como 500 em vez de 503.
        stub = self._stub
        if stub is not None:
            return stub
        with self._lock:
            if self._stub is None:
                try:
                    import grpc  # type: ignore
                    from app.premium.proto import premium_pb2_grpc  # type: ignore
                except ImportError as exc:
                    raise PremiumIndisponivelError(
                        "PREMIUM_ENABLED=true mas o transporte gRPC não está disponível. "
                        "Instalar `grpcio` e gerar os stubs de premium.proto."
                    ) from exc
                self._canal = self._criar_canal(grpc)
                bruto = getattr(premium_pb2_grpc, self._NOME_STUB)(self._canal)
                self._stub = _StubComDescarte(bruto, self)
                self._canais_montados += 1
                # O primeiro canal é o arranque normal e não interessa a ninguém.
                # Do segundo em diante houve uma reconstrução, e essa é a linha
                # que falta a quem tem de perceber porque é que o premium
                # respondeu 503: diz se o canal chegou a ser substituído.
                registar = (logger.warning if self._canais_montados > 1
                            else logger.info)
                registar(
                    "canal para o sidecar montado (#%d desde o arranque deste "
                    "processo; %d descarte(s), %d recusado(s) pelo ritmo)",
                    self._canais_montados, self._descartes,
                    self._descartes_travados,
                )
            return self._stub

    def _criar_canal(self, grpc):
        return criar_canal_sidecar(grpc, self._addr)

    def _descartar_canal(self) -> None:
        """Larga o canal atual para o pedido seguinte montar um novo.

        Porquê: o canal do gRPC reconecta sozinho, mas com recuo progressivo, e o
        recuo compõe-se entre falhas sucessivas. Medido: depois de o sidecar ser
        reposto **pela segunda vez** na mesma vida do processo, o canal ficava a
        recusar durante mais de quatro minutos — enquanto um canal acabado de
        criar, no mesmo contentor, alcançava o sidecar de imediato. Na prática,
        reiniciar o sidecar obrigava a reiniciar o núcleo.

        O travão de tempo é o que impede a cura de virar problema: durante uma
        paragem longa há um pedido a falhar por segundo, e sem ele cada um deles
        montaria um canal novo.
        """
        agora = time.monotonic()
        with self._lock:
            if agora - self._ultimo_descarte < self._INTERVALO_RECONEXAO:
                self._descartes_travados += 1
                return
            self._ultimo_descarte = agora
            self._descartes += 1
            canal, self._canal, self._stub = self._canal, None, None
        # WARNING e não INFO: o sidecar deixou de ser alcançável, e o ritmo do
        # travão limita isto a uma linha por segundo mesmo numa paragem longa.
        logger.warning(
            "canal para o sidecar descartado após falha de transporte "
            "(descarte #%d; %d recusado(s) pelo ritmo desde o arranque)",
            self._descartes, self._descartes_travados,
        )
        if canal is not None:
            # O canal é partilhado: fechá-lo já cancelava as chamadas de outros
            # pedidos que ainda corriam nele (uma importação a meio recebia
            # CANCELLED). Fecha-se depois de uma folga maior do que a operação
            # mais longa; entretanto os pedidos novos já usam o canal novo.
            fecho = threading.Timer(self._GRACA_FECHO_S, self._fechar_canal, args=(canal,))
            fecho.daemon = True
            fecho.start()

    @staticmethod
    def _fechar_canal(canal) -> None:
        try:
            canal.close()
        except Exception:  # noqa: BLE001 — já se está a desistir deste canal
            pass

    def _invocar(self, chamada, *args, _nome_rpc: str = "", **kwargs):
        """Faz a chamada e, se o transporte falhar, larga o canal.

        Não se chama à mão: quem lhe chega vem do `_StubComDescarte`, que envolve
        **todos** os RPC do stub. É essa a garantia de que um método novo não
        nasce sem reconexão.

        Uma leitura que falhe por o canal estar morto repete-se **uma vez**, já no
        canal novo. Sem isto, o primeiro pedido depois de o sidecar reiniciar
        respondia 503 sempre — o canal era trocado, mas só o pedido seguinte
        beneficiava, e o ecrã abria com um erro que um F5 fazia desaparecer.

        As leituras levam `PRAZO_LEITURA_S` quando quem chama não deu prazo.
        """
        if _e_leitura(_nome_rpc):
            kwargs.setdefault("timeout", PRAZO_LEITURA_S)
        try:
            return chamada(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — reenviada a seguir
            if not e_indisponibilidade(exc):
                raise
            if not _canal_morto(exc):
                # Prazo esgotado: o sidecar está ocupado, não em baixo. O canal
                # serve; largá-lo não curava nada e afetava os outros pedidos.
                raise
            self._descartar_canal()
            if not _repetivel(_nome_rpc, exc):
                raise
            logger.info("leitura %s repetida no canal remontado", _nome_rpc)
            novo = self._ensure_stub()
            return getattr(novo._stub, _nome_rpc)(*args, **kwargs)


class GrpcTransport(ClienteSidecar, PremiumTransport):
    """
    Liga ao sidecar premium via gRPC (premium.v1).

    Requer `grpcio` instalado e os stubs gerados a partir de
    app/premium/proto/premium.proto (`premium_pb2` / `premium_pb2_grpc`).

    Transporte: canal mTLS com pinning da CA a partir de PREMIUM_TLS_CA/CLIENT_CERT/
    CLIENT_KEY (ver _criar_canal). Sem esse material o canal é recusado — fail-closed.
    """

    _NOME_STUB = "PremiumProviderStub"

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().CheckEntitlement(
            premium_pb2.EntitlementQuery(tenant_id=tenant_id, feature=feature),
        )
        return Entitlement(
            feature=feature,
            enabled=resp.enabled,
            limits=dict(resp.limits),
            expires_at=resp.expires_at or None,
            reason=resp.reason,
        )

    def estado_licenca(self, tenant_id: str, nif: str = "") -> EstadoLicenca:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().EstadoLicenca(
            premium_pb2.EstadoLicencaReq(tenant_id=tenant_id, nif=nif or "")
        )
        return EstadoLicenca(
            estado=resp.estado,
            plano=resp.plano,
            expires_at=resp.expires_at or None,
            grace_ate=resp.grace_ate or None,
            dias_restantes=resp.dias_restantes,
            codigo_instalacao=resp.codigo_instalacao,
            instance_id=resp.instance_id,
            heartbeat_estado=resp.heartbeat_estado,
            heartbeat_ultimo_ok=resp.heartbeat_ultimo_ok or None,
            dias_sem_heartbeat=resp.dias_sem_heartbeat,
            so_leitura=resp.so_leitura,
            so_leitura_motivo=resp.so_leitura_motivo,
        )

    def instalar_licenca(
        self, envelope_json: str, nif: str, so_validar: bool, tenant_id: str
    ) -> LicencaInstalada:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().InstalarLicenca(
            premium_pb2.InstalarLicencaReq(
                envelope_json=envelope_json, nif=nif or "", so_validar=bool(so_validar),
                tenant_id=tenant_id or "",
            )
        )
        return LicencaInstalada(
            aceite=resp.aceite,
            codigo_erro=resp.codigo_erro,
            detalhe=resp.detalhe,
            customer=resp.customer,
            nif_licenca=resp.nif_licenca,
            plano=resp.plano,
            expires_at=resp.expires_at or None,
            grace_dias=resp.grace_dias,
            modulos=list(resp.modulos),
            license_id=resp.license_id,
            instalada=resp.instalada,
        )

    # --- Assistente IA (o sidecar é dono do job) ---

    # Chunk do payload de evidências (256 KiB) — contorna o limite de 4 MB do gRPC.
    _CHUNK_BYTES = 256 * 1024
    # Prazo do envio da análise. Quem o espera segura a única vaga das operações
    # pesadas do núcleo (ver solicitar_analise): sem prazo, um sidecar pendurado
    # prendia também os dossiês e os backups de todos. O sidecar espera pelo
    # gateway até 120 s (mais 10 s para ligar); isto dá-lhe folga para acabar.
    PRAZO_ANALISE_S = 180.0

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        import grpc  # type: ignore
        from app.premium.proto import premium_pb2  # type: ignore

        stub = self._ensure_stub()

        def _gen():
            # 1ª mensagem: metadata. Seguintes: chunks de evidências (cifradas).
            yield premium_pb2.ChunkContexto(
                meta=premium_pb2.MetaContexto(
                    tenant_id=meta["tenant_id"],
                    controlo_empresa_id=meta["controlo_empresa_id"],
                    framework_id=meta["framework_id"],
                    controlo_codigo=meta["controlo_codigo"],
                    nivel_minimo=meta["nivel_minimo"],
                    locale=meta["locale"],
                    idempotency_key=meta.get("idempotency_key", ""),
                    pedido_por=meta.get("pedido_por", ""),
                )
            )
            for i in range(0, len(evidencias), self._CHUNK_BYTES):
                yield premium_pb2.ChunkContexto(
                    evidencia=evidencias[i : i + self._CHUNK_BYTES]
                )

        try:
            resp = stub.CriarAnaliseGaps(_gen(), timeout=self.PRAZO_ANALISE_S)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise AnaliseLimiteError(_detalhe_429(exc.details() or "")) from exc
            raise
        job = _job_pb_to_dict(resp)
        if job is None:
            raise RuntimeError("sidecar devolveu job vazio na submissão")
        return job

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool
    ) -> dict | None:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterAnalisePorControlo(
            premium_pb2.AnaliseControloRef(
                tenant_id=tenant_id,
                controlo_empresa_id=controlo_empresa_id,
                reclamar_auditoria=reclamar_auditoria,
            )
        )
        return _job_pb_to_dict(resp)


# Teto de entradas da cache de direitos (tenant × feature).
_CACHE_MAX = 4096


class PremiumClient:
    """
    Fachada do core para o premium. Resolve o transporte por config e faz cache
    curta (TTL) das verificações de entitlement.
    """

    def __init__(self, transport: PremiumTransport, cache_ttl: int) -> None:
        self._transport = transport
        self._cache_ttl = max(0, cache_ttl)
        self._cache: dict[tuple[str, str], tuple[float, Entitlement]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True se há um transporte real (sidecar configurado)."""
        return not isinstance(self._transport, NullTransport)

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        key = (tenant_id, feature)
        now = time.monotonic()
        if self._cache_ttl:
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit[0] > now:
                    return hit[1]
        ent = self._transport.check_entitlement(tenant_id, feature)
        if not ent.enabled and ent.reason == "lookup_error":
            # O sidecar não conseguiu ler os direitos: não se sabe se o tenant
            # tem o módulo. Não fica em cache, e chega a quem pede como
            # indisponibilidade (503), não como «falta pagar» (402).
            raise PremiumIndisponivelError(f"direitos ilegíveis no sidecar ({feature})")
        if self._cache_ttl:
            with self._lock:
                self._cache[key] = (now + self._cache_ttl, ent)
                if len(self._cache) > _CACHE_MAX:
                    # Com muitos tenants a cache crescia sem fim: saem os vencidos
                    # e, se não chegar, os mais antigos.
                    for k in [k for k, (ate, _) in self._cache.items() if ate <= now]:
                        del self._cache[k]
                    if len(self._cache) > _CACHE_MAX:
                        for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[: len(self._cache) // 2]:
                            del self._cache[k]
        return ent

    def has_feature(self, tenant_id: str, feature: str) -> bool:
        return self.check_entitlement(tenant_id, feature).enabled

    def estado_licenca(self, tenant_id: str, nif: str = "") -> EstadoLicenca:
        """Estado agregado da licença. Sem cache: é barato e o cartão quer o valor
        do momento (um cliente que acabou de renovar não deve ver o estado antigo)."""
        return self._transport.estado_licenca(tenant_id, nif)

    def instalar_licenca(
        self, envelope_json: str, nif: str, so_validar: bool, tenant_id: str
    ) -> LicencaInstalada:
        """Instalar/validar um ficheiro de licença. Sem cache: muda o estado."""
        return self._transport.instalar_licenca(envelope_json, nif, so_validar, tenant_id)

    # --- Assistente IA — pass-through ao transporte (sem cache; é estado de job) ---

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        return self._transport.criar_analise_gaps(meta, evidencias)

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool = False
    ) -> dict | None:
        return self._transport.obter_analise_por_controlo(
            tenant_id, controlo_empresa_id, reclamar_auditoria
        )


def _build_transport() -> PremiumTransport:
    settings = get_settings()
    if not settings.PREMIUM_ENABLED:
        return NullTransport()
    if not settings.PREMIUM_SIDECAR_ADDR:
        raise RuntimeError(
            "PREMIUM_ENABLED=true mas PREMIUM_SIDECAR_ADDR está vazio. "
            "Definir o endereço do sidecar premium (ex.: premium-sidecar:50051)."
        )
    return GrpcTransport(settings.PREMIUM_SIDECAR_ADDR)


# Os clientes partilhados, um por (classe, endereço). Montar um canal mTLS custa
# ler três ficheiros PEM e um aperto de mãos TLS: quem chama muitas vezes (a
# pesquisa, a cada tecla) não o pode fazer a cada chamada.
_PARTILHADOS: dict[tuple[type, str], "ClienteSidecar"] = {}
_LOCK_PARTILHADOS = threading.Lock()


def cliente_partilhado(classe: type, addr: str):
    """O cliente de `classe` para `addr`: um só por processo, com um só canal."""
    with _LOCK_PARTILHADOS:
        cliente = _PARTILHADOS.get((classe, addr))
        if cliente is None:
            cliente = _PARTILHADOS[(classe, addr)] = classe(addr)
        return cliente


@lru_cache
def get_premium_client() -> PremiumClient:
    """Singleton do PremiumClient. Usar como dependência FastAPI: Depends(get_premium_client)."""
    settings = get_settings()
    return PremiumClient(_build_transport(), settings.PREMIUM_ENTITLEMENT_CACHE_TTL)
