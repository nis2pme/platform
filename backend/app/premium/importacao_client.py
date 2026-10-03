"""
Cliente gRPC do módulo de Importação (premium.v1 / ImportacaoService).

Isolado, reutiliza o MESMO canal mTLS ao sidecar (`criar_canal_sidecar`). O core
é passthrough: o sidecar é a autoridade — lê o ficheiro, normaliza, reconcilia e
persiste, porque é dono da premium-data-db onde os ativos e os riscos vivem.

O conteúdo do ficheiro segue em *stream*, como o backup já faz: o core não o
interpreta, não o guarda e não o escreve em lado nenhum. O que fica do lado de
cá é o registo de auditoria — quem carregou o quê, quando, e com que impressão
digital.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterator

from app.config import get_settings
from app.premium.client import ClienteSidecar

# Tamanho de cada bloco enviado ao sidecar. Confortavelmente abaixo do limite de
# mensagem do gRPC e grande o suficiente para não fragmentar de mais.
TAMANHO_BLOCO = 256 * 1024


# ── Conversões protobuf → dict ────────────────────────────────────────────────

def _ator_pb(premium_pb2, ator: dict | None):
    if not ator:
        return None
    return premium_pb2.Ator(
        id=ator.get("id", ""),
        nome=ator.get("nome", ""),
        ambito=ator.get("ambito", ""),
    )


def _mapeamento_to_dict(pb) -> dict:
    return {
        "colunas": [
            {"coluna": c.coluna, "campo": c.campo, "chave": c.chave} for c in pb.colunas
        ],
        "formato_data": pb.formato_data,
        "separador_decimal": pb.separador_decimal,
        "equivalencias": dict(pb.equivalencias),
        "vazio_apaga": pb.vazio_apaga,
    }


def _mapeamento_pb(premium_pb2, mapeamento: dict | None):
    """dict do frontend → protobuf. Ausente = o sidecar usa o que já guardou."""
    if mapeamento is None:
        return None
    return premium_pb2.Mapeamento(
        colunas=[
            premium_pb2.ColunaMapeada(
                coluna=str(c.get("coluna", "")),
                campo=str(c.get("campo", "")),
                chave=bool(c.get("chave", False)),
            )
            for c in mapeamento.get("colunas", [])
        ],
        formato_data=str(mapeamento.get("formato_data", "")),
        separador_decimal=str(mapeamento.get("separador_decimal", "")),
        equivalencias={
            str(k): str(v) for k, v in (mapeamento.get("equivalencias") or {}).items()
        },
        vazio_apaga=bool(mapeamento.get("vazio_apaga", False)),
    )


def _campo_to_dict(pb) -> dict:
    return {
        "chave": pb.chave,
        "label": pb.label,
        "ajuda": pb.ajuda,
        "obrigatorio": pb.obrigatorio,
        "tipo_dado": pb.tipo_dado,
        "sinonimos": list(pb.sinonimos),
        "opcoes": list(pb.opcoes),
        "equivalencias": dict(pb.equivalencias),
    }


def _problema_to_dict(pb) -> dict:
    return {
        "linha": pb.linha,
        "codigo": pb.codigo,
        "coluna": pb.coluna,
        "valor": pb.valor,
        "detalhe": pb.detalhe,
        # Aviso ou impedimento. O mesmo código diz as duas coisas conforme a
        # coluna onde aconteceu, e é o ecrã que precisa de os separar.
        "bloqueia": pb.bloqueia,
    }


def _diff_to_dict(pb) -> dict:
    return {
        "importacao_id": pb.importacao_id,
        "novos": pb.novos,
        "atualizados": pb.atualizados,
        "ignorados": pb.ignorados,
        "ausentes": pb.ausentes,
        "bloqueados": pb.bloqueados,
        "problemas": [_problema_to_dict(p) for p in pb.problemas],
        "reconciliacao": [
            {
                "linha": r.linha,
                "rotulo": r.rotulo,
                "candidatos": [
                    {
                        "id": c.id,
                        "nome": c.nome,
                        "motivo": c.motivo,
                        "confianca": c.confianca,
                    }
                    for c in r.candidatos
                ],
            }
            for r in pb.reconciliacao
        ],
        "travoes": list(pb.travoes),
        "pode_aplicar": pb.pode_aplicar,
        "carimbo": pb.carimbo,
    }


def _resumo_to_dict(pb) -> dict:
    return {
        "importacao_id": pb.importacao_id,
        "estado": pb.estado,
        "criados": pb.criados,
        "atualizados": pb.atualizados,
        "ignorados": pb.ignorados,
        "marcados_ausentes": pb.marcados_ausentes,
        "saltados": pb.saltados,
        "problemas": [_problema_to_dict(p) for p in pb.problemas],
    }


def _importacao_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "fonte": pb.fonte,
        "destino": pb.destino,
        "ambito": pb.ambito,
        "nome_ficheiro": pb.nome_ficheiro,
        "estado": pb.estado,
        "linhas": pb.linhas,
        "criados": pb.criados,
        "atualizados": pb.atualizados,
        "data_origem": pb.data_origem or None,
        "criado_em": pb.criado_em,
        "autor_nome": pb.autor_nome,
        "versao_perfil": pb.versao_perfil,
        "reversivel": pb.reversivel,
    }


def _perfil_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "fonte": pb.fonte,
        "nome": pb.nome,
        "mapeamento": _mapeamento_to_dict(pb.mapeamento),
        "atualizado_em": pb.atualizado_em or None,
    }


def _descoberta_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "fonte": pb.fonte,
        "endereco": pb.endereco,
        "rotulo": pb.rotulo,
        "estado": pb.estado,
        "ativo_id": pb.ativo_id or None,
        "primeiro_visto": pb.primeiro_visto,
        "ultimo_visto": pb.ultimo_visto,
        "vezes_vista": pb.vezes_vista,
    }


def _alteracao_to_dict(pb) -> dict:
    return {
        "importacao_id": pb.importacao_id,
        "fonte": pb.fonte,
        "nome_ficheiro": pb.nome_ficheiro,
        "autor_nome": pb.autor_nome,
        "criado": pb.criado,
        "valores_antes": dict(pb.valores_antes),
        "escrito_em": pb.escrito_em,
    }


def _transferencia_to_dict(pb) -> dict:
    return {
        "tipo": pb.tipo,
        "valor": pb.valor,
        "fonte": pb.fonte,
        "quando": pb.quando,
        "perdida": pb.perdida,
        "outro_id": pb.outro_id,
        "outro_nome": pb.outro_nome,
    }


def _fonte_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "nome": pb.nome,
        "descricao": pb.descricao,
        "destino": pb.destino,
        "versao_perfil": pb.versao_perfil,
        "formatos": list(pb.formatos),
        "campos": [_campo_to_dict(c) for c in pb.campos],
        "ajuda_exportacao": pb.ajuda_exportacao,
        # A empresa tem o módulo onde esta fonte escreve? Vem à mesma na lista,
        # para o ecrã a mostrar esbatida com a razão em vez de a esconder.
        "disponivel": pb.disponivel,
        "modulo_em_falta": pb.modulo_em_falta or None,
    }


def _analise_to_dict(pb) -> dict:
    return {
        "importacao_id": pb.importacao_id,
        "formato": pb.formato,
        "delimitador": pb.delimitador,
        "codificacao": pb.codificacao,
        "linhas_lidas": pb.linhas_lidas,
        "bytes_lidos": pb.bytes_lidos,
        "cabecalhos": list(pb.cabecalhos),
        "amostra": [list(l.celulas) for l in pb.amostra],
        "mapeamento_sugerido": _mapeamento_to_dict(pb.mapeamento_sugerido),
        "fonte_sugerida": pb.fonte_sugerida,
        "confianca": pb.confianca,
        "avisos": list(pb.avisos),
        "ja_importado": pb.ja_importado,
        "ja_importado_em": pb.ja_importado_em or None,
    }


class ImportacaoClient(ClienteSidecar):
    """Fala com o ImportacaoService do sidecar. Stub criado de forma lazy."""

    _NOME_STUB = "ImportacaoServiceStub"

    def listar_fontes(self, tenant_id: str, locale: str = "", destino: str = "") -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarFontes(
            premium_pb2.FontesReq(tenant_id=tenant_id, locale=locale, destino=destino)
        )
        return [_fonte_to_dict(f) for f in resp.fontes]

    def analisar_ficheiro(
        self,
        tenant_id: str,
        conteudo: bytes,
        meta: dict,
        ator: dict | None = None,
        timeout: int = 120,
    ) -> dict:
        """Envia o ficheiro em blocos e devolve a análise (sem escrever nada)."""
        from app.premium.proto import premium_pb2  # type: ignore

        cabecalho = premium_pb2.MetaImportacao(
            tenant_id=tenant_id,
            fonte=meta.get("fonte", ""),
            destino=meta.get("destino", ""),
            ambito=meta.get("ambito", ""),
            nome_ficheiro=meta.get("nome_ficheiro", ""),
            sha256=meta.get("sha256", ""),
            locale=meta.get("locale", ""),
            ator=_ator_pb(premium_pb2, ator),
        )

        def blocos() -> Iterator:
            # Cabeçalho primeiro — é o contrato do stream, e é ele que traz o
            # tenant que o sidecar usa no portão de direito.
            yield premium_pb2.ImportacaoChunk(meta=cabecalho)
            for i in range(0, len(conteudo), TAMANHO_BLOCO):
                yield premium_pb2.ImportacaoChunk(
                    dados=conteudo[i : i + TAMANHO_BLOCO]
                )

        resp = self._ensure_stub().AnalisarFicheiro(blocos(), timeout=timeout)
        return _analise_to_dict(resp)

    def importar_observacao(
        self,
        tenant_id: str,
        conteudo: bytes,
        meta: dict,
        perfil: str,
        declaracoes: dict[str, str],
        ator: dict | None = None,
        travoes_confirmados: list[str] | None = None,
        timeout: int = 180,
    ) -> dict:
        """Relatório técnico → verificação no motor de conetores do sidecar.

        Leva o contexto do core (nível QNRCS + o que está declarado) pela mesma
        razão que a verificação em linha: é o que permite a régua por nível e a
        deteção de contradições entre o declarado e o observado.

        Os travões confirmados viajam no cabeçalho porque esta classe não tem
        passo de simulação onde os confirmar: um relatório recusado volta pelo
        mesmo caminho, agora com a confirmação de quem o quis mesmo carregar.
        """
        return self._enviar_relatorio(
            "ImportarObservacao", tenant_id, conteudo, meta, perfil, declaracoes,
            ator, travoes_confirmados, timeout,
        )

    def importar_coletor_ad(
        self,
        tenant_id: str,
        conteudo: bytes,
        meta: dict,
        perfil: str,
        declaracoes: dict[str, str],
        ator: dict | None = None,
        travoes_confirmados: list[str] | None = None,
        timeout: int = 180,
    ) -> dict:
        """Ficheiro do coletor do Active Directory → verificação da fonte "ad".

        Mesmo transporte e mesmo resumo das observações; o sidecar lê-o com o
        leitor do coletor e verifica o direito do conetor AD.
        """
        return self._enviar_relatorio(
            "ImportarColetorAd", tenant_id, conteudo, {**meta, "fonte": "ad"}, perfil,
            declaracoes, ator, travoes_confirmados, timeout,
        )

    def _enviar_relatorio(
        self,
        metodo: str,
        tenant_id: str,
        conteudo: bytes,
        meta: dict,
        perfil: str,
        declaracoes: dict[str, str],
        ator: dict | None,
        travoes_confirmados: list[str] | None,
        timeout: int,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        cabecalho = premium_pb2.MetaImportacao(
            tenant_id=tenant_id,
            fonte=meta.get("fonte", ""),
            destino="observacao",
            nome_ficheiro=meta.get("nome_ficheiro", ""),
            sha256=meta.get("sha256", ""),
            locale=meta.get("locale", ""),
            ator=_ator_pb(premium_pb2, ator),
            perfil=perfil,
            declaracoes=declaracoes,
            travoes_confirmados=travoes_confirmados or [],
        )

        def blocos() -> Iterator:
            yield premium_pb2.ImportacaoChunk(meta=cabecalho)
            for i in range(0, len(conteudo), TAMANHO_BLOCO):
                yield premium_pb2.ImportacaoChunk(dados=conteudo[i : i + TAMANHO_BLOCO])

        resp = getattr(self._ensure_stub(), metodo)(blocos(), timeout=timeout)
        return {
            "importacao_id": resp.importacao_id,
            "verificacao_id": resp.verificacao_id,
            "observado_em": resp.observado_em or None,
            "sinais_avaliados": resp.sinais_avaliados,
            "nao_conformes_politica": resp.nao_conformes_politica,
            "nao_conformes_minimo": resp.nao_conformes_minimo,
            "indeterminados": resp.indeterminados,
            "eventos_novos": resp.eventos_novos,
            "duplicado": resp.duplicado,
            "avisos": list(resp.avisos),
            "ativos_sem_cobertura": resp.ativos_sem_cobertura,
            # A lista tem teto (nomes para mostrar); o total é que diz quantas
            # máquinas o relatório viu mesmo fora do inventário.
            "descobertas": list(resp.descobertas),
            "total_descobertas": resp.total_descobertas,
        }

    # ── Simular / aplicar / reverter ─────────────────────────────────────────

    def simular(
        self,
        tenant_id: str,
        importacao_id: str,
        mapeamento: dict | None,
        ator: dict | None = None,
        timeout: int = 120,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().SimularImportacao(
            premium_pb2.SimulacaoReq(
                tenant_id=tenant_id,
                importacao_id=importacao_id,
                mapeamento=_mapeamento_pb(premium_pb2, mapeamento),
                ator=_ator_pb(premium_pb2, ator),
            ),
            timeout=timeout,
        )
        return _diff_to_dict(resp)

    def aplicar(
        self,
        tenant_id: str,
        importacao_id: str,
        mapeamento: dict | None,
        decisoes: list[dict],
        carimbo: str,
        travoes_confirmados: list[str],
        ator: dict | None = None,
        timeout: int = 180,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().AplicarImportacao(
            premium_pb2.AplicarReq(
                tenant_id=tenant_id,
                importacao_id=importacao_id,
                mapeamento=_mapeamento_pb(premium_pb2, mapeamento),
                decisoes=[
                    premium_pb2.DecisaoReconciliacao(
                        linha=int(d.get("linha", 0)),
                        acao=str(d.get("acao", "")),
                        alvo_id=str(d.get("alvo_id", "")),
                    )
                    for d in decisoes
                ],
                carimbo=carimbo,
                travoes_confirmados=list(travoes_confirmados),
                ator=_ator_pb(premium_pb2, ator),
            ),
            timeout=timeout,
        )
        return _resumo_to_dict(resp)

    def reverter(
        self, tenant_id: str, importacao_id: str, ator: dict | None = None, timeout: int = 120
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ReverterImportacao(
            premium_pb2.ImportacaoRef(
                tenant_id=tenant_id, id=importacao_id, ator=_ator_pb(premium_pb2, ator)
            ),
            timeout=timeout,
        )
        return _resumo_to_dict(resp)

    # ── Descobertas e história do registo ────────────────────────────────────

    def listar_descobertas(self, tenant_id: str, estado: str = "", limite: int = 100) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarDescobertas(
            premium_pb2.DescobertasReq(tenant_id=tenant_id, estado=estado, limite=limite)
        )
        return {
            "descobertas": [_descoberta_to_dict(d) for d in resp.descobertas],
            "pendentes": resp.pendentes,
        }

    def decidir_descobertas(
        self, tenant_id: str, ids: list[str], acao: str, ator: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().DecidirDescobertas(
            premium_pb2.DecisaoDescobertasReq(
                tenant_id=tenant_id,
                ids=list(ids),
                acao=acao,
                ator=_ator_pb(premium_pb2, ator),
            )
        )
        return {
            "descobertas": [_descoberta_to_dict(d) for d in resp.descobertas],
            "pendentes": resp.pendentes,
        }

    def documento(self, tenant_id: str, locale: str = "") -> dict:
        """Evidência: de onde vieram os dados e quando. Gerada a pedido, como
        todos os documentos premium."""
        from app.premium.inventario_client import _documento_to_dict
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().DocumentoImportacoes(
            premium_pb2.DocumentoReq(tenant_id=tenant_id, tipo="importacoes", locale=locale)
        )
        return _documento_to_dict(resp)

    def qualidade(self, tenant_id: str) -> dict:
        """Quantos ativos têm dono, série e procedência, e há quanto tempo foram
        confirmados. Percentagem vazia = inventário vazio (e não 0%)."""
        from app.premium.proto import premium_pb2  # type: ignore

        r = self._ensure_stub().QualidadeInventario(
            premium_pb2.QualidadeReq(tenant_id=tenant_id)
        )
        pct = lambda v: int(v) if v else None  # noqa: E731 — vazio tem significado
        return {
            "total": r.total,
            "com_dono": r.com_dono,
            "com_serie": r.com_serie,
            "com_procedencia": r.com_procedencia,
            "ausentes": r.ausentes,
            "por_confirmar": r.por_confirmar,
            "vistos_30d": r.vistos_30d,
            "pct_com_dono": pct(r.pct_com_dono),
            "pct_com_serie": pct(r.pct_com_serie),
            "pct_com_procedencia": pct(r.pct_com_procedencia),
            "pct_vistos_30d": pct(r.pct_vistos_30d),
        }

    def historico_registo(self, tenant_id: str, entidade_id: str, destino: str) -> dict:
        """História deste registo: o que as importações lhe mudaram e que
        identificadores ele perdeu para outro registo (ou ganhou de outro).

        As duas coisas vêm juntas porque respondem à mesma pergunta na ficha —
        *porque é que isto está assim?* — e separá-las obrigaria o ecrã a fazer
        dois pedidos para montar uma linha temporal só.
        """
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().HistoricoRegisto(
            premium_pb2.HistoricoRegistoReq(
                tenant_id=tenant_id, entidade_id=entidade_id, destino=destino
            )
        )
        return {
            "alteracoes": [_alteracao_to_dict(a) for a in resp.alteracoes],
            "transferencias": [_transferencia_to_dict(t) for t in resp.transferencias],
        }

    # ── Histórico e perfis ───────────────────────────────────────────────────

    def listar_importacoes(
        self,
        tenant_id: str,
        fonte: str = "",
        destino: str = "",
        limite: int = 20,
        offset: int = 0,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarImportacoes(
            premium_pb2.HistoricoReq(
                tenant_id=tenant_id,
                fonte=fonte,
                destino=destino,
                limite=limite,
                offset=offset,
            )
        )
        return {
            "importacoes": [_importacao_to_dict(i) for i in resp.importacoes],
            "total": resp.total,
        }

    def detalhe(self, tenant_id: str, importacao_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().DetalheImportacao(
            premium_pb2.ImportacaoRef(tenant_id=tenant_id, id=importacao_id)
        )
        return {
            "importacao": _importacao_to_dict(resp.importacao),
            "problemas": [_problema_to_dict(p) for p in resp.problemas],
            "mapeamento": _mapeamento_to_dict(resp.mapeamento),
            "sha256": resp.sha256,
        }

    def guardar_perfil(
        self,
        tenant_id: str,
        fonte: str,
        nome: str,
        mapeamento: dict,
        ator: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().GuardarPerfilMapeamento(
            premium_pb2.PerfilMapeamento(
                tenant_id=tenant_id,
                fonte=fonte,
                nome=nome,
                mapeamento=_mapeamento_pb(premium_pb2, mapeamento),
                ator=_ator_pb(premium_pb2, ator),
            )
        )
        return _perfil_to_dict(resp)

    def listar_perfis(self, tenant_id: str, fonte: str = "") -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarPerfisMapeamento(
            premium_pb2.PerfisReq(tenant_id=tenant_id, fonte=fonte)
        )
        return [_perfil_to_dict(p) for p in resp.perfis]


@lru_cache
def get_importacao_client() -> ImportacaoClient | None:
    """Singleton do cliente de importação, ou None se o premium estiver desligado."""
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    return ImportacaoClient(settings.PREMIUM_SIDECAR_ADDR)
