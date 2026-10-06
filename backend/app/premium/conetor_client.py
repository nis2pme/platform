"""
Cliente gRPC das verificações técnicas (premium.v1 / ConetorService).

Isolado (fácil de manter), reutiliza o MESMO canal mTLS ao sidecar
(`criar_canal_sidecar`). O core é passthrough: o sidecar é a autoridade (valida,
cifra as credenciais em repouso, faz o tenant-scoping e é dono da
premium-data-db). Nenhuma lógica de domínio aqui, e nenhuma fonte escrita à mão:
quem chama diz sempre de que fonte fala.

Nota de segurança: as credenciais só passam por aqui EM TRÂNSITO (canal mTLS),
ao configurar uma ligação. Nenhum método as devolve, o core nunca as guarda nem
as escreve em logs.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import get_settings
from app.premium.client import ClienteSidecar
from app.premium.conversao import ator_pb

# As features das verificações técnicas (uma por ligação ou ferramenta). O core
# só precisa de as conhecer para o portão "tem alguma?" do router e do tick:
# que fonte exige que feature decide-o o sidecar, que filtra o que devolve.
FEATURES_CONETORES = ("connector_m365", "connector_ad", "connector_gvm", "connector_wazuh")


# ── Conversões protobuf → dict ────────────────────────────────────────────────


def _estado_to_dict(pb) -> dict:
    return {
        "configurado": pb.configurado,
        "tipo": pb.tipo,
        "ms_tenant_id": pb.ms_tenant_id,
        "client_id": pb.client_id,
        "credencial_tipo": pb.credencial_tipo,
        "credencial_expira_em": pb.credencial_expira_em or None,
        "intervalo_horas": pb.intervalo_horas,
        "ativo": pb.ativo,
        "sinais_config_json": pb.sinais_config_json,
        "ultima_verificacao": pb.ultima_verificacao or None,
        "ultimo_resultado": pb.ultimo_resultado,
        "modo": pb.modo,
        "dominio": pb.dominio,
        # Ligação direta ao AD: servidor, porta, nome_tls, utilizador (nunca a password).
        "parametros": dict(pb.parametros),
        "tem_ca": pb.tem_ca,
    }


def _sinal_to_dict(pb) -> dict:
    return {
        "sinal": pb.sinal,
        "fonte": pb.fonte,
        "veredicto_minimo": pb.veredicto_minimo,
        "veredicto_politica": pb.veredicto_politica,
        "razao": pb.razao,
        "resumo_json": pb.resumo_json,
        "controlos": list(pb.controlos),
        "verificado_em": pb.verificado_em or None,
        # Na língua pedida. Nas listas vem resumido (título e classificação);
        # os passos completos vêm no detalhe.
        "remediacao_json": pb.remediacao_json,
    }


def _evento_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "fonte": pb.fonte,
        "sinal": pb.sinal,
        "tipo": pb.tipo,
        "de_veredicto": pb.de_veredicto,
        "para_veredicto": pb.para_veredicto,
        "criado_em": pb.criado_em,
        "resolvido_em": pb.resolvido_em or None,
        "resolvido_por": pb.resolvido_por,
        "nota": pb.nota,
        "controlos": list(pb.controlos),
    }


def _estado_fonte_to_dict(pb) -> dict:
    return {
        "fonte": pb.fonte,
        "tema": pb.tema,
        "transportes": list(pb.transportes),
        "transporte": pb.transporte,
        "configurado": pb.configurado,
        "ultimo_dado": pb.ultimo_dado or None,
        "idade_dias": pb.idade_dias if pb.idade_dias >= 0 else None,
        "idade_maxima_dias": pb.idade_maxima_dias or None,
        "dados_velhos": pb.dados_velhos,
        "ultima_verificacao": pb.ultima_verificacao or None,
        "ultimo_resultado": pb.ultimo_resultado,
        "ultimo_erro": pb.ultimo_erro,
        "tem_metas": pb.tem_metas,
        "dominio": pb.dominio,
    }


def _facto_to_dict(pb) -> dict:
    try:
        dados = json.loads(pb.dados_json or "{}")
    except ValueError:
        dados = {}
    return {
        "ativo_id": pb.ativo_id,
        "ativo_nome": pb.ativo_nome,
        "fonte": pb.fonte,
        "dominio": pb.dominio,
        "dados": dados,
        "observado_em": pb.observado_em or None,
        "criticidade": pb.criticidade,
        "estado_ativo": pb.estado_ativo,
    }


def _alerta_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "fonte": pb.fonte,
        "agente": pb.agente,
        "titulo": pb.titulo,
        "nivel": pb.nivel,
        "ocorrido_em": pb.ocorrido_em or None,
        "ativo_id": pb.ativo_id or None,
        "ativo_nome": pb.ativo_nome or None,
        "estado": pb.estado,
        "motivo": pb.motivo,
        "incidente_id": pb.incidente_id or None,
        "decidido_por": pb.decidido_por,
        "decidido_em": pb.decidido_em or None,
        "recebido_em": pb.recebido_em,
    }


class ConetorClient(ClienteSidecar):
    """Fala com o ConetorService do sidecar. Stub criado de forma lazy."""

    _NOME_STUB = "ConetorServiceStub"

    # ── Ligações ──────────────────────────────────────────────────────────────

    def configurar(self, tenant_id: str, tipo: str, dados: dict, ator: dict | None = None) -> dict:
        """Ligação em linha com identificadores e segredo (Microsoft 365)."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ConfigurarConetor(
            premium_pb2.ConfigConetorReq(
                tenant_id=tenant_id,
                tipo=tipo,
                modo="direto",
                ms_tenant_id=dados.get("ms_tenant_id", ""),
                client_id=dados.get("client_id", ""),
                credencial=dados.get("credencial", ""),
                credencial_tipo=dados.get("credencial_tipo", ""),
                intervalo_horas=int(dados.get("intervalo_horas") or 0),
                # Vazio = manter as metas guardadas (editam-se à parte).
                sinais_config_json=dados.get("sinais_config_json", ""),
                ativo=bool(dados.get("ativo", True)),
                ator=ator_pb(premium_pb2, ator),
            )
        )
        return _estado_to_dict(resp)

    def configurar_ligacao_direta(
        self,
        tenant_id: str,
        tipo: str,
        parametros: dict[str, str],
        ca_pem: str,
        senha: str,
        intervalo_horas: int,
        ativo: bool,
        ator: dict | None = None,
    ) -> dict:
        """Ligação direta por servidor e conta de serviço (o AD por LDAPS). Senha
        e CA vazias = manter as guardadas. A senha só viaja pelo canal mTLS até
        ao sidecar, que a cifra."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ConfigurarConetor(
            premium_pb2.ConfigConetorReq(
                tenant_id=tenant_id,
                tipo=tipo,
                modo="direto",
                parametros=parametros,
                ca_pem=ca_pem,
                credencial=senha,
                credencial_tipo="secret",
                intervalo_horas=intervalo_horas,
                ativo=ativo,
                ator=ator_pb(premium_pb2, ator),
            )
        )
        return _estado_to_dict(resp)

    def voltar_ao_ficheiro(self, tenant_id: str, tipo: str, ator: dict | None = None) -> dict:
        """Desliga a ligação direta de uma fonte que também chega por ficheiro: o
        sidecar apaga a password, a CA e os parâmetros; sinais e histórico ficam."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ConfigurarConetor(
            premium_pb2.ConfigConetorReq(
                tenant_id=tenant_id, tipo=tipo, modo="ficheiro", ativo=True,
                ator=ator_pb(premium_pb2, ator),
            )
        )
        return _estado_to_dict(resp)

    def estado(self, tenant_id: str, tipo: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().EstadoConetor(premium_pb2.ConetorTenantReq(tenant_id=tenant_id, tipo=tipo))
        return _estado_to_dict(resp)

    def testar(self, tenant_id: str, tipo: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().TestarConetor(premium_pb2.ConetorTenantReq(tenant_id=tenant_id, tipo=tipo))
        return {"ok": resp.ok, "erro_categoria": resp.erro_categoria, "dominio": resp.dominio}

    def executar_verificacao(
        self,
        tenant_id: str,
        tipo: str,
        perfil: str,
        declaracoes: dict[str, str],
        ator: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ExecutarVerificacao(
            premium_pb2.VerificacaoConetorReq(
                tenant_id=tenant_id,
                perfil=perfil,
                declaracoes=declaracoes,
                ator=ator_pb(premium_pb2, ator),
                tipo=tipo,
            )
        )
        return {
            "verificacao_id": resp.verificacao_id,
            "resultado": resp.resultado,
            "erro_categoria": resp.erro_categoria,
            "sinais_avaliados": resp.sinais_avaliados,
            "nao_conformes_politica": resp.nao_conformes_politica,
            "nao_conformes_minimo": resp.nao_conformes_minimo,
            "indeterminados": resp.indeterminados,
            "eventos_novos": resp.eventos_novos,
        }

    def remover(self, tenant_id: str, tipo: str, ator: dict | None = None) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().RemoverConetor(
            premium_pb2.RemoverConetorReq(tenant_id=tenant_id, ator=ator_pb(premium_pb2, ator), tipo=tipo)
        )
        return {"ok": True}

    def obter_coletor(self, tenant_id: str, tipo: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterColetorAd(premium_pb2.ConetorTenantReq(tenant_id=tenant_id, tipo=tipo))
        return {
            "script": bytes(resp.script),
            "sha256": resp.sha256,
            "versao": resp.versao,
            "nome_ficheiro": resp.nome_ficheiro,
        }

    # ── Metas da empresa ──────────────────────────────────────────────────────

    def configurar_politica(
        self, tenant_id: str, tipo: str, sinais_config_json: str, ativo: bool = True,
        ator: dict | None = None,
    ) -> dict:
        """As metas da empresa para os sinais de uma fonte. O modo "politica" não
        mexe no modo nem na ligação (um AD em ligação direta continua ligado)."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ConfigurarConetor(
            premium_pb2.ConfigConetorReq(
                tenant_id=tenant_id,
                tipo=tipo,
                modo="politica",
                sinais_config_json=sinais_config_json,
                ativo=ativo,
                ator=ator_pb(premium_pb2, ator),
            )
        )
        return _estado_to_dict(resp)

    def reavaliar_observacoes(self, tenant_id: str, perfil: str, declaracoes: dict[str, str]) -> dict:
        """Reavalia o que depende do tempo ou do nível do perfil (a idade de cada
        leitura ou relatório) com o contexto atual."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ReavaliarObservacoes(
            premium_pb2.VerificacaoConetorReq(tenant_id=tenant_id, perfil=perfil, declaracoes=declaracoes)
        )
        return {
            "sinais_reavaliados": resp.sinais_avaliados,
            "nao_conformes": resp.nao_conformes_politica,
            "eventos_novos": resp.eventos_novos,
        }

    # ── Leituras ──────────────────────────────────────────────────────────────

    def catalogo(self, tenant_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().CatalogoVerificacoes(premium_pb2.ConetorTenantReq(tenant_id=tenant_id))
        return json.loads(resp.catalogo_json or "{}")

    def estado_fontes(self, tenant_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().EstadoFontes(premium_pb2.ConetorTenantReq(tenant_id=tenant_id))
        return {
            "fontes": [_estado_fonte_to_dict(f) for f in resp.fontes],
            # Identidades: "local" | "cloud" | "hibrido" | "independentes" | "".
            "cenario": resp.cenario,
            "avisos_cenario": list(resp.avisos_cenario),
        }

    def constatacoes(
        self, tenant_id: str, tema: str = "", locale: str = "", declaracoes: dict | None = None
    ) -> dict:
        """`declaracoes` (código do controlo → estado declarado) deixa o sidecar
        apurar as contradições com o estado de agora."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().Constatacoes(
            premium_pb2.ConstatacoesReq(
                tenant_id=tenant_id, tema=tema, locale=locale, declaracoes=declaracoes or {}
            )
        )
        return {"sinais": [_sinal_to_dict(s) for s in resp.sinais]}

    def constatacoes_dos_controlos(
        self, tenant_id: str, codigos: list[str], locale: str = "", declaracoes: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ConstatacoesDosControlos(
            premium_pb2.ControlosConstatacoesReq(
                tenant_id=tenant_id, codigos=codigos, locale=locale, declaracoes=declaracoes or {}
            )
        )
        return {"sinais": [_sinal_to_dict(s) for s in resp.sinais]}

    def avisos_do_risco(
        self,
        tenant_id: str,
        codigos: list[str],
        ativo_id: str = "",
        locale: str = "",
        declaracoes: dict | None = None,
    ) -> dict:
        """O que as verificações dizem de um risco: os controlos (por código) que
        falham a meta e, havendo ativo, as vulnerabilidades graves por corrigir."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().AvisosDoRisco(
            premium_pb2.AvisosRiscoReq(
                tenant_id=tenant_id,
                codigos=codigos,
                ativo_id=ativo_id,
                locale=locale,
                declaracoes=declaracoes or {},
            )
        )
        return {
            "controlos_a_falhar": [
                {"codigo": c.codigo, "sinais": [_sinal_to_dict(s) for s in c.sinais]}
                for c in resp.controlos_a_falhar
            ],
            "ativo": (
                {"criticas": resp.ativo.criticas, "observado_em": resp.ativo.observado_em}
                if resp.HasField("ativo")
                else None
            ),
        }

    def detalhe_sinal(
        self, tenant_id: str, fonte: str, sinal: str, ator: dict | None = None, locale: str = ""
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().DetalheSinal(
            premium_pb2.DetalheSinalReq(
                tenant_id=tenant_id, sinal=sinal, ator=ator_pb(premium_pb2, ator), fonte=fonte, locale=locale
            )
        )
        return {"sinal": _sinal_to_dict(resp.sinal), "detalhe_json": resp.detalhe_json}

    def listar_eventos(
        self,
        tenant_id: str,
        a_partir_de: int = 0,
        limite: int = 0,
        so_por_resolver: bool = False,
        recentes_primeiro: bool = False,
        antes_de: int = 0,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarEventosConetor(
            premium_pb2.EventosConetorReq(
                tenant_id=tenant_id,
                a_partir_de=a_partir_de,
                limite=limite,
                so_por_resolver=so_por_resolver,
                recentes_primeiro=recentes_primeiro,
                antes_de=antes_de,
            )
        )
        return {"eventos": [_evento_to_dict(e) for e in resp.eventos]}

    def resolver_evento(self, tenant_id: str, evento_id: int, nota: str = "", ator: dict | None = None) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().ResolverEventoConetor(
            premium_pb2.ResolverEventoConetorReq(
                tenant_id=tenant_id, evento_id=evento_id, nota=nota, ator=ator_pb(premium_pb2, ator)
            )
        )
        return {"ok": True}

    def processamento(
        self, tenant_id: str, a_partir_de: int = 0, verificacoes_desde: str = "", limite: int = 0
    ) -> dict:
        """Para o tick: eventos novos, as constatações para a evidência (com os
        controlos onde anexar) e as execuções agendadas concluídas."""
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ProcessamentoConetores(
            premium_pb2.ProcessamentoReq(
                tenant_id=tenant_id,
                a_partir_de=a_partir_de,
                verificacoes_desde=verificacoes_desde,
                limite=limite,
            )
        )
        return {
            "eventos": [_evento_to_dict(e) for e in resp.eventos],
            "constatacoes": [
                {
                    "fonte": c.fonte,
                    "sinal": c.sinal,
                    "veredicto_minimo": c.veredicto_minimo,
                    "veredicto_politica": c.veredicto_politica,
                    "razao": c.razao,
                    "controlos": list(c.controlos),
                    "controlos_evidencia": list(c.controlos_evidencia),
                    "corpo_evidencia_json": c.corpo_evidencia_json,
                    "verificado_em": c.verificado_em or None,
                }
                for c in resp.constatacoes
            ],
            "verificacoes": [
                {
                    "fonte": v.fonte,
                    "iniciada_em": v.iniciada_em,
                    "resultado": v.resultado,
                    "erro_categoria": v.erro_categoria,
                    "transporte": v.transporte,
                }
                for v in resp.verificacoes
            ],
        }

    def factos_do_ativo(self, tenant_id: str, ativo_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().FactosDoAtivo(premium_pb2.FactosAtivoReq(tenant_id=tenant_id, ativo_id=ativo_id))
        return {"factos": [_facto_to_dict(f) for f in resp.factos]}

    def factos_por_dominio(self, tenant_id: str, dominio: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().FactosPorDominio(premium_pb2.FactosDominioReq(tenant_id=tenant_id, dominio=dominio))
        return {"factos": [_facto_to_dict(f) for f in resp.factos]}

    def listar_alertas(self, tenant_id: str, estado: str = "", limite: int = 0) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarAlertas(
            premium_pb2.AlertasReq(tenant_id=tenant_id, estado=estado, limite=limite)
        )
        return {
            "alertas": [_alerta_to_dict(a) for a in resp.alertas],
            "por_analisar": resp.por_analisar,
            "por_analisar_atrasados": resp.por_analisar_atrasados,
        }

    def decidir_alerta(
        self,
        tenant_id: str,
        alerta_id: int,
        decisao: str,
        motivo: str = "",
        incidente_id: str = "",
        ator: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().DecidirAlerta(
            premium_pb2.DecidirAlertaReq(
                tenant_id=tenant_id,
                id=alerta_id,
                decisao=decisao,
                motivo=motivo,
                incidente_id=incidente_id,
                ator=ator_pb(premium_pb2, ator),
            )
        )
        return _alerta_to_dict(resp)


@lru_cache
def get_conetor_client() -> ConetorClient | None:
    """Singleton do cliente, ou None se o premium estiver desligado."""
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    return ConetorClient(settings.PREMIUM_SIDECAR_ADDR)
