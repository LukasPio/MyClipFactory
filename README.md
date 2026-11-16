# MyClipFactory

Estúdio local para transformar gravações autorizadas de gameplay em MP4 ou WebM em clipes verticais legendados. O painel acompanha as gravações, as renderizações, a revisão por versão e os envios que o operador autoriza em cada conta.

## Instalação

Requisitos para Linux: Python 3.12+, FFmpeg/FFprobe com libx264, AAC e libass, e as fontes DejaVu Sans ou equivalentes selecionadas no painel. Para a suíte de mídia, o FFmpeg também precisa do filtro `flite`. As instalações Debian/Ubuntu podem usar `sudo apt-get install ffmpeg fonts-dejavu-core fonts-liberation fonts-noto-core`.

```bash
git clone https://github.com/LukasPio/MyClipFactory.git
cd MyClipFactory
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cp .env.example .env
```

No primeiro terminal, inicie a API e o painel:

```bash
.venv/bin/python -m uvicorn clipfactory.api:app --host 127.0.0.1 --port 8000 --no-access-log
```

No segundo terminal, a partir do mesmo diretório, inicie o worker:

```bash
.venv/bin/python -m clipfactory.pipeline
```

Abra [http://127.0.0.1:8000](http://127.0.0.1:8000). Ambos os processos devem usar o mesmo diretório de dados. O Faster Whisper baixa os pesos no primeiro uso. `MCF_WHISPER_MODEL=tiny` reduz o custo de CPU para experimentar. O padrão `small` prioriza transcrição mais precisa. Também é possível apontar para o caminho local de um modelo CTranslate2 e operar offline. Para desempenho em GPU, configure `MCF_WHISPER_DEVICE=cuda`, o tipo de cálculo compatível e as bibliotecas exigidas pelo CTranslate2.

## Operação

1. Em **Vídeos**, confirme a autorização de uso e envie o arquivo MP4 ou WebM com pelo menos uma faixa de áudio. A inspeção lista duração, FPS, resolução, codec e faixas disponíveis para análise.
2. Clique em **Processar** e ajuste duração, quantidade, idioma e faixa de áudio. A webcam pode ser detectada automaticamente ou indicada com as coordenadas percentuais da sobreposição.
3. A fila exibe etapas registradas pelo worker. É possível cancelar, consultar logs e tentar de novo. Decodificação do áudio e do vídeo precede a transcrição.
4. Em **Cortes**, revise o score, a justificativa e o MP4. Ajuste o intervalo, o enquadramento, os metadados e as legendas. Corrija a transcrição na mesma tela. Ao salvar, o worker renderiza somente o corte usando a análise persistida.
5. Aprove o clipe e selecione uma conta. Revise os metadados específicos, a visibilidade quando aplicável e a data opcional. Autorize o destino. Para enviar a contas adicionais, repita a autorização para cada uma.
6. Em **Publicação**, acompanhe estados, erros, referências remotas e links retornados. É possível retirar a autorização antes de o worker iniciar o envio. Depois de publicar, registre os números da plataforma para comparação no histórico.

Os limites padrão são 30 GiB por arquivo e 12 horas de duração. O upload usa fluxo para disco e mostra bytes enviados. O progresso do worker indica etapas concluídas, não uma previsão de tempo restante. O worker processa um job de mídia por vez e, em paralelo, atende a fila de publicações.

## Análise e edição

O modo padrão combina transcrições e timestamps do Faster Whisper, variações de áudio, movimento, transições visuais e faces persistentes. Usa evidências de fala e de imagem, alinhamento ao começo/fim das falas quando possível, pontuação de 0 a 100 e supressão de janelas sobrepostas. Os sinais locais são aproximações editoriais. Por exemplo, uma mudança visual pode sugerir uma cena e a palavra “explodiu” pode sustentar uma hipótese, mas inferir eventos, humor ou expressões só a partir desses sinais é limitado. Gravações sem evidência suficiente podem gerar zero clipes. A detecção automática favorece rostos persistentes nas bordas. Use a região manual quando necessário.

Para uma segunda avaliação contextual, habilite um modelo visual já instalado no Ollama com `MCF_VISION_MODEL` e `MCF_VISION_ORIGIN`. Ele recebe três quadros e as falas do candidato e devolve avaliação estruturada, tipo de evento, justificativa e preferência de enquadramento. O servidor escolhido recebe essas imagens e falas, por isso a configuração padrão usa um serviço local e o recurso vem desligado.

Os clipes usam H.264/AAC, 1080 × 1920 e até 60 FPS conforme a origem. A composição usa gameplay, gameplay com janela da webcam e layouts divididos com ênfase temporal na reação. O rastreamento facial usa amostras e centralização por trecho. Legendas usam timestamps por palavra quando disponíveis. Segmentos de baixa confiança ficam marcados, e os mais incertos aparecem como `[inaudível]`. Uma correção que altera o número de palavras distribui o tempo dentro do segmento original e pede revisão visual.

Trilhas autorizadas são opcionais. Guarde os arquivos em `data/music/` e crie `data/music/library.json`:

```json
[
  {"file":"leve.ogg","mood":"funny","license":"Autorização da faixa para edição e republicação"},
  {"file":"atmosfera.ogg","mood":"tension","license":"Licença da faixa"},
  {"file":"ritmo.ogg","mood":"epic","license":"Licença da faixa"}
]
```

O worker escolhe a primeira faixa cujo estilo corresponda ao candidato. Reações intensas e candidatos sem estilo compatível usam o áudio original. O FFmpeg aplica redução da trilha durante a fala e normalização com limitador.

## Plataformas

Crie suas próprias aplicações nos consoles oficiais. Gere `MCF_TOKEN_KEY` conforme o exemplo e mantenha a mesma chave para recuperar tokens e sessões entre execuções. Configure ID e segredo por plataforma. Registre `<MCF_ORIGIN>/api/oauth/<plataforma>/callback`, trocando `<plataforma>` por `youtube`, `tiktok` ou `instagram`. Use a mesma URL no painel e no console. Para expor o painel, configure `MCF_PASSWORD`, use **operator** como usuário e termine TLS num proxy confiável, mantendo o processo Uvicorn privado. Desabilite logs de query string no proxy para callbacks OAuth e mídia assinada.

| Destino | Dependências e comportamento |
| --- | --- |
| YouTube Shorts | Habilite YouTube Data API v3. O OAuth autoriza o canal e a aplicação usa upload retomável, consulta do processamento e validação de privacidade. Por padrão oferece envio privado. Use `MCF_YOUTUBE_AUDITED=1` para habilitar outras opções somente após a auditoria aplicável. [Protocolo de upload](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol) |
| TikTok | Configure Login Kit e Content Posting API com `video.publish`. Direct Post vem desabilitado. Habilite `MCF_TIKTOK_DIRECT_POST=1` apenas para uma aplicação que se enquadre nas diretrizes. A plataforma restringe ferramentas internas de envio a contas próprias e exige auditoria para postagem pública. Em instalação acessada em outro host, use `MCF_TIKTOK_PULL_FROM_URL=1` com `MCF_MEDIA_HTTPS_ORIGIN` verificada no console do TikTok. Sem auditoria, a API só oferece a opção privada para contas elegíveis. [Diretrizes do TikTok](https://developers.tiktok.com/docs/en/content-sharing-guidelines) |
| Instagram Reels | Configure Instagram API with Instagram Login para uma conta profissional com permissões de publicação. Configure `MCF_MEDIA_HTTPS_ORIGIN` para uma origem pública HTTPS cujo proxy encaminhe o caminho `/media/` para o serviço. A URL individual é assinada, expira em uma hora e permite leitura durante a publicação. O adaptador acompanha o contêiner até o envio e consulta o permalink. [Publicação de conteúdo do Instagram](https://developers.facebook.com/documentation/instagram-platform/content-publishing) |

Sem acesso a uma integração, baixe o MP4 pelo painel. A fila persiste consentimento, destino e revisão, e grava sessões de upload e IDs remotos antes de retomar. Erros transitórios recebem retentativas limitadas conforme o provedor, enquanto resultados ambíguos podem exigir consulta no painel da plataforma. Alterar ou rejeitar um clipe revoga autorizações pendentes daquele corte. O envio real exige contas, credenciais e permissões externas. A suíte usa respostas HTTP de teste para os três provedores e não cria publicações.

## Testes

```bash
python -m pytest -q
# Inclui o teste do modelo real com a gravação gerada:
MCF_TEST_WHISPER=1 MCF_TEST_MODEL=tiny python -m pytest -q
```

A gravação de teste é sintetizada no momento da execução, com ação visual, uma região de webcam desenhada, voz, pausas e reações. A suíte verifica ingestão, corrupção, retomada de fila, timestamps, cortes, dimensões e codecs, correção de legendas, revisão por versão, agendamento, aprovação por conta, OAuth e contratos HTTP. O teste do modelo real pode usar um caminho local em `MCF_TEST_MODEL`.

## Escopo da instalação

Esta versão usa FastAPI, HTML/CSS/JavaScript e SQLite em WAL, com processos de API e worker separados e armazenamento em disco. A instalação atende um operador e permite várias contas de destino. PostgreSQL, fila distribuída, múltiplos usuários, fontes ao vivo, coleta automática de métricas e armazenamento remoto seguem como extensões. Histórico e métricas manuais são persistidos. Os modelos editoriais e os exemplos de transcrição não substituem a revisão de um clipe real.

Veja [arquitetura, contratos e decisões](docs/architecture.md).
