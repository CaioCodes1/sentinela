# Diagramas

As fontes em Mermaid ficam aqui, isoladas, para poderem ser reaproveitadas
(slide, apresentação, outra ferramenta) sem recortar de dentro de um documento.
As versões renderizadas aparecem nos documentos que as usam — o GitHub renderiza
Mermaid direto no Markdown.

| Arquivo | Onde aparece |
|---|---|
| `arquitetura.mmd` | [README](../../README.md#arquitetura) |
| `fluxo-requisicao.mmd` | [README](../../README.md#arquitetura) |
| `fluxo-autenticacao.mmd` | [README](../../README.md#segurança) |
| `fluxo-automacao.mmd` | [AUTOMACAO.md](../AUTOMACAO.md) |
| `estados-contrato.mmd` | [AUTOMACAO.md](../AUTOMACAO.md) |
| `modelo-dados.mmd` | [BANCO-DE-DADOS.md](../BANCO-DE-DADOS.md) |

Para gerar imagem estática, se for preciso:

```bash
npx -y @mermaid-js/mermaid-cli -i arquitetura.mmd -o arquitetura.svg
```
