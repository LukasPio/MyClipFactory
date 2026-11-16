"use strict";

const get = (id) => document.getElementById(id);
const platformNames = {youtube: "YouTube Shorts", tiktok: "TikTok", instagram: "Instagram Reels"};
const labels = {
  QUEUED: "Na fila", RUNNING: "Em processamento", DONE: "Concluído", FAILED: "Falhou",
  CANCELLED: "Cancelado", PENDING: "A revisar", APPROVED: "Aprovado", REJECTED: "Rejeitado",
  SCHEDULED: "Agendado", PROCESSING: "Em publicação", PUBLISHED: "Publicado",
};
const initial = {videos: [], clips: [], jobs: [], accounts: [], publications: [], integrations: {}, metrics: []};
let library = initial;
let timer;

function node(tag, options = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (value == null) continue;
    if (key in element && key !== "form") element[key] = value;
    else element.setAttribute(key, value);
  }
  element.append(...children.filter((value) => value != null));
  return element;
}

function button(label, action, options = {}) {
  const el = node("button", {type: "button", ...options}, label);
  el.addEventListener("click", async () => {
    try { await action(); } catch (error) { flash(error.message, true); }
  });
  return el;
}

function flash(message, error = false) {
  const notice = get("notice");
  notice.textContent = message;
  notice.hidden = false;
  notice.classList.toggle("error", error);
  clearTimeout(timer);
  timer = setTimeout(() => { notice.hidden = true; }, error ? 10000 : 5500);
}

function responseError(data, fallback) {
  if (Array.isArray(data.detail)) {
    return data.detail.map((error) => error.msg).join(" · ");
  }
  return data.detail || fallback;
}

async function api(path, method = "GET", payload) {
  const options = {method};
  if (payload !== undefined) {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(payload);
  }
  const response = await fetch("/api" + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(responseError(data, "A ação falhou."));
  return data;
}

function viewTo(name) {
  for (const part of ["videos", "clips", "publishing"]) get(part).hidden = part !== name;
  document.querySelectorAll(".nav").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === name);
    if (el.dataset.view === name) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  document.title = `${get(name).querySelector("h1").textContent} · MyClipFactory`;
}

function seconds(value) {
  const whole = Math.max(0, Math.round(value));
  const mins = String(Math.floor(whole / 60)).padStart(2, "0");
  return `${mins}:${String(whole % 60).padStart(2, "0")}`;
}

function date(value) {
  return new Intl.DateTimeFormat("pt-BR", {dateStyle: "short", timeStyle: "short"}).format(value * 1000);
}

function badge(state) {
  const style = ["DONE", "PUBLISHED", "APPROVED"].includes(state) ? " good"
    : ["FAILED", "REJECTED", "CANCELLED"].includes(state) ? " bad" : "";
  return node("span", {className: "badge" + style}, labels[state] || state);
}

function renderList(id, rows, empty) {
  get(id).replaceChildren(...(rows.length ? rows : [node("p", {className: "empty"}, empty)]));
}

function dialog(title, body, submit, onSubmit) {
  const modal = get("dialog");
  const form = get("dialog-form");
  get("dialog-title").textContent = title;
  const error = node("p", {className: "form-error", role: "alert", hidden: true});
  const action = submit ? node("button", {type: "submit", className: "primary"}, submit) : null;
  form.replaceChildren(...body, error, ...(action ? [node("div", {className: "actions"}, action)] : []));
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!onSubmit) return;
    action.disabled = true;
    error.hidden = true;
    try {
      await onSubmit(form);
      modal.close();
      await refresh();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      action.disabled = false;
    }
  };
  if (!modal.open) modal.showModal();
}

function field(label, input) {
  return node("label", {className: input.type === "checkbox" ? "consent" : "field"},
              ...(input.type === "checkbox" ? [input, label] : [label, input]));
}

function input(name, type, value, options = {}) {
  return node("input", {name, type, value, ...options});
}

function select(name, entries, value) {
  const el = node("select", {name}, ...entries.map(([key, label]) => node("option", {value: key}, label)));
  el.value = value;
  return el;
}

function row(...controls) {
  return node("div", {className: "form-row"}, ...controls);
}

function textArea(name, value, maxLength) {
  return node("textarea", {name, value, maxLength, rows: 2});
}

function text(form, name) {
  return form.elements.namedItem(name).value.trim();
}

function checked(form, name) {
  return form.elements.namedItem(name)?.checked || false;
}

function number(form, name) {
  return Number(text(form, name));
}

function videoRows() {
  const rows = library.videos.map((video) => {
    const details = `${seconds(video.info.duration)} · ${video.info.width}×${video.info.height} · `
      + `${video.info.fps.toFixed(2)} FPS · ${video.info.codec.toUpperCase()} · ${(video.bytes / 1048576).toFixed(1)} MB`;
    const running = library.jobs.some((job) => job.video_id === video.id && job.kind === "VIDEO"
      && ["QUEUED", "RUNNING"].includes(job.state));
    return node("article", {className: "item"},
      node("div", {className: "item-main"}, node("h3", {}, video.name), node("p", {className: "meta"}, details)),
      button(running ? "Analisando" : "Processar", () => processDialog(video), {disabled: running}));
  });
  renderList("video-list", rows, "Nenhuma gravação enviada.");
  get("video-count").textContent = String(rows.length);
}

function processDialog(video) {
  const coords = ["x", "y", "w", "h"].map((key, index) => field(["Esquerda", "Topo", "Largura", "Altura"][index],
    input(key, "number", "", {min: "0", max: "100", step: ".1", placeholder: "%"})));
  const track = select("audio_stream", video.info.audio.map((a) =>
    [String(a.index), `Faixa ${a.index} · ${a.codec} · ${a.language}`]), String(video.info.audio[0].index));
  dialog("Processar gravação", [
    node("p", {}, video.name),
    row(field("Duração mínima (s)", input("min_seconds", "number", "20", {required: true, min: 5, max: 60})),
        field("Duração máxima (s)", input("max_seconds", "number", "60", {required: true, min: 5, max: 90})),
        field("Máximo de cortes", input("limit", "number", "6", {required: true, min: 1, max: 20}))),
    row(field("Idioma", select("language", [["auto", "Detectar"], ["pt", "Português"], ["en", "Inglês"]], "auto")),
        field("Faixa de áudio", track)),
    field("Nomes, termos e gírias para o transcritor", input("vocabulary", "text", "", {maxLength: 500})),
    field("Detectar a webcam automaticamente", input("detect_webcam", "checkbox", "", {checked: true})),
    node("details", {}, node("summary", {}, "Definir região da webcam manualmente"), row(...coords),
      node("p", {className: "meta"}, "Informe as quatro medidas em porcentagem a partir do canto superior esquerdo.")),
  ], "Iniciar análise", async (form) => {
    const keys = ["x", "y", "w", "h"];
    const manual = keys.some((key) => text(form, key) !== "");
    if (manual && !keys.every((key) => text(form, key) !== "")) throw new Error("Preencha as quatro medidas da webcam.");
    await api(`/videos/${video.id}/process`, "POST", {
      min_seconds: number(form, "min_seconds"), max_seconds: number(form, "max_seconds"), limit: number(form, "limit"),
      language: text(form, "language"), audio_stream: number(form, "audio_stream"), vocabulary: text(form, "vocabulary"),
      detect_webcam: checked(form, "detect_webcam"),
      webcam: manual ? Object.fromEntries(keys.map((key) => [key, number(form, key) / 100])) : null,
    });
    flash("Gravação enviada à fila.");
  });
}

async function jobEvents(job) {
  const events = await api(`/jobs/${job.id}/events`);
  const entries = events.length ? events.map((event) =>
    node("p", {}, `${date(event.stamp)} · ${event.progress}% · ${event.message}`)) : [node("p", {}, "Sem eventos registrados.")];
  dialog("Histórico do processamento", entries);
}

function jobRows() {
  const list = library.jobs.map((job) => {
    const video = library.videos.find((item) => item.id === job.video_id);
    const clip = library.clips.find((item) => item.id === job.clip_id);
    const active = ["QUEUED", "RUNNING"].includes(job.state);
    const progress = node("progress", {max: 100, value: job.progress, "aria-label": "Progresso do processamento"});
    const actions = node("div", {className: "actions"}, badge(job.state), button("Logs", () => jobEvents(job)));
    if (active && !job.cancel) actions.append(button("Cancelar", async () => {
      await api(`/jobs/${job.id}/cancel`, "POST");
      await refresh();
    }));
    if (["FAILED", "CANCELLED"].includes(job.state)
      && (!clip || clip.revision === job.config.revision)) actions.append(button("Tentar de novo", async () => {
      await api(`/jobs/${job.id}/retry`, "POST");
      await refresh();
    }));
    return node("article", {className: "item"}, node("div", {className: "item-main"},
      node("h3", {}, `${job.kind === "VIDEO" ? "Análise" : "Renderização"} · ${clip?.fields.title || video?.name || ""}`),
      node("p", {}, job.message || "Aguardando worker."),
      node("p", {className: "meta"}, progress, ` ${job.progress}%`)), actions);
  });
  renderList("jobs", list, "A fila está vazia.");
  get("job-count").textContent = String(list.length);
}

function metadataControls(fields) {
  return [
    field("Título", input("title", "text", fields.title, {required: true, maxLength: 100})),
    field("Descrição", textArea("description", fields.description, 2000)),
    field("Hashtags, separadas por vírgula", input("hashtags", "text", fields.hashtags.join(", "), {maxLength: 820})),
    field("Texto de abertura", input("hook", "text", fields.hook, {maxLength: 180})),
    field("Legenda para redes sociais", textArea("caption", fields.caption, 2000)),
  ];
}

function metadata(form) {
  const hashtags = text(form, "hashtags").split(",").map((tag) => tag.trim().replace(/^#+/, "")).filter(Boolean);
  return {title: text(form, "title"), description: text(form, "description"), hashtags,
    hook: text(form, "hook"), caption: text(form, "caption")};
}

async function editDialog(clip) {
  const source = library.videos.find((video) => video.id === clip.video_id);
  const values = clip.fields;
  const transcript = await api(`/clips/${clip.id}/transcript`);
  const corrections = new Map(values.corrections.map((fix) => [fix.id, fix.text]));
  const segmentList = node("div", {className: "transcript"});
  const start = input("start", "number", values.start, {min: 0, max: source.info.duration, step: "any", required: true});
  const end = input("end", "number", values.end, {min: 0, max: source.info.duration, step: "any", required: true});
  function segments() {
    const visible = transcript.segments.filter((entry) => entry.end > Number(start.value) && entry.start < Number(end.value));
    segmentList.replaceChildren(...(visible.length ? visible.map((entry) => {
      const description = `${seconds(entry.start)}–${seconds(entry.end)}${entry.review ? " · Revisar confiança" : ""}`;
      const original = entry.unclear ? "[inaudível]" : entry.text;
      const edit = textArea(`segment-${entry.id}`, corrections.get(entry.id) ?? original, 1200);
      edit.addEventListener("input", () => {
        if (edit.value === original) corrections.delete(entry.id);
        else corrections.set(entry.id, edit.value);
      });
      return field(description, edit);
    }) : [node("p", {}, "Sem fala transcrita neste intervalo.")]));
  }
  start.addEventListener("change", segments);
  end.addEventListener("change", segments);
  segments();
  const style = values.captions;
  const modes = [["auto", "Automático"], ["gameplay", "Gameplay"]];
  if (clip.candidate.webcam) modes.push(["split", "Composição dividida"], ["webcam", "Webcam em destaque"]);
  dialog("Editar corte", [
    ...metadataControls(values), row(field("Início no original (s)", start), field("Fim no original (s)", end),
                                    field("Enquadramento", select("layout", modes, values.layout))),
    node("details", {}, node("summary", {}, "Estilo das legendas"),
      row(field("Fonte", select("font", ["DejaVu Sans", "Liberation Sans", "Noto Sans"].map((f) => [f, f]), style.font)),
          field("Tamanho", input("size", "number", style.size, {min: 28, max: 95, required: true})),
          field("Palavras por bloco", input("words", "number", style.words, {min: 2, max: 8, required: true}))),
      row(field("Texto", input("color", "color", style.color)), field("Destaque", input("highlight", "color", style.highlight)),
          field("Posição", select("position", [["safe", "Área segura"], ["bottom", "Mais abaixo"]], style.position))),
      field("Destacar palavras conforme a fala", input("animate", "checkbox", "", {checked: style.animate}))),
    node("details", {}, node("summary", {}, "Corrigir transcrição"), segmentList),
  ], "Salvar e renderizar", async (form) => {
    await api(`/clips/${clip.id}?revision=${clip.revision}`, "PUT", {
      ...metadata(form), start: number(form, "start"), end: number(form, "end"), layout: text(form, "layout"),
      captions: {font: text(form, "font"), size: number(form, "size"), words: number(form, "words"),
        color: text(form, "color"), highlight: text(form, "highlight"), position: text(form, "position"), animate: checked(form, "animate")},
      corrections: Array.from(corrections, ([id, value]) => ({id, text: value})),
    });
    flash("Nova edição adicionada à fila.");
  });
}

async function review(clip, decision) {
  await api(`/clips/${clip.id}/review`, "POST", {decision, revision: clip.revision});
  await refresh();
}

function renderClips() {
  const filter = get("review-filter").value;
  const existing = new Map(Array.from(get("clip-list").children).filter((el) => el.dataset.id).map((el) => [el.dataset.id, el]));
  const rows = library.clips.filter((clip) => !filter || clip.state === filter).map((clip) => {
    const activeRender = library.jobs.some((job) => job.clip_id === clip.id && ["QUEUED", "RUNNING"].includes(job.state));
    const display = JSON.stringify([clip, activeRender, library.accounts.length]);
    if (existing.get(clip.id)?.dataset.display === display) return existing.get(clip.id);
    const ready = clip.revision === clip.rendered_revision;
    const url = `/api/clips/${clip.id}/video`;
    const preview = ready ? node("video", {controls: true, playsInline: true, preload: "metadata", src: url})
      : node("div", {className: "preview-missing"}, activeRender ? "Renderização na fila ou em andamento" : "MP4 indisponível. Consulte a fila.");
    const settings = node("div", {className: "actions"}, button("Editar", () => editDialog(clip), {disabled: activeRender}));
    if (ready) {
      settings.append(node("a", {href: url + "?download=true", className: "button"}, "Baixar MP4"));
      if (clip.state !== "APPROVED") settings.append(button("Aprovar", () => review(clip, "APPROVED"), {className: "primary"}));
      if (clip.state !== "REJECTED") settings.append(button("Rejeitar", () => review(clip, "REJECTED")));
      if (clip.state === "APPROVED" && library.accounts.length) {
        settings.append(button("Selecionar destino", () => destinationDialog(clip), {className: "primary"}));
      }
    }
    const card = node("article", {className: "clip"}, preview,
      node("div", {className: "clip-top"}, badge(clip.state), node("span", {className: "score"}, `${clip.candidate.score}/100`)),
      node("h3", {}, clip.fields.title),
      node("p", {className: "meta"}, `${seconds(clip.fields.start)}–${seconds(clip.fields.end)} · ${seconds(clip.fields.end - clip.fields.start)} · v${clip.revision}`),
      node("p", {}, clip.fields.description),
      node("p", {className: "meta"}, clip.fields.hashtags.map((tag) => "#" + tag).join(" ")),
      node("details", {}, node("summary", {}, "Por que foi selecionado"), node("p", {}, clip.candidate.reason)),
      settings);
    card.dataset.id = clip.id;
    card.dataset.display = display;
    const oldVideo = existing.get(clip.id)?.querySelector("video");
    if (oldVideo && ready && existing.get(clip.id)?.dataset.revision === String(clip.revision)) {
      preview.replaceWith(oldVideo);
    }
    card.dataset.revision = String(clip.revision);
    return card;
  });
  const parent = get("clip-list");
  if (!rows.length) {
    if (!parent.querySelector(".empty")) renderList("clip-list", [], "Nenhum corte disponível para este filtro.");
    return;
  }
  const desired = new Set(rows);
  for (const child of [...parent.children]) if (!desired.has(child)) child.remove();
  rows.forEach((card, index) => {
    if (parent.children[index] !== card) parent.insertBefore(card, parent.children[index] || null);
  });
}

function destinationDialog(clip) {
  const accounts = library.accounts.map((account) => [account.id, `${platformNames[account.platform]} · ${account.label}`]);
  const selector = select("account", [["", "Selecione uma conta"], ...accounts], "");
  dialog("Destino do corte", [field("Conta", selector)]);
  selector.addEventListener("change", async () => {
    try { await publicationDialog(clip, library.accounts.find((a) => a.id === selector.value)); }
    catch (error) { flash(error.message, true); get("dialog").close(); }
  });
}

async function publicationDialog(clip, account) {
  if (!account) return;
  const fields = {...clip.fields};
  if (account.platform === "tiktok" || account.platform === "instagram") {
    fields.caption ||= fields.title;
  }
  const options = [];
  let updateDisclosure;
  if (account.platform === "youtube") {
    options.push(field("Visibilidade", select("privacy", [["private", "Privado"], ["unlisted", "Não listado"], ["public", "Público"]], "private")));
  }
  if (account.platform === "tiktok") {
    const info = await api(`/accounts/${account.id}/creator`);
    options.push(node("p", {}, `Criador: ${info.nickname || info.username}. Máximo: ${info.max_duration}s.`));
    const privacyLabels = {SELF_ONLY: "Somente eu", PUBLIC_TO_EVERYONE: "Público",
      MUTUAL_FOLLOW_FRIENDS: "Amigos mútuos", FOLLOWER_OF_CREATOR: "Seguidores"};
    const privacy = select("privacy", [["", "Escolha a visibilidade"], ...info.privacy.map((key) =>
      [key, privacyLabels[key] || key])], "");
    privacy.required = true;
    options.push(field("Visibilidade", privacy));
    if ((clip.fields.end - clip.fields.start) > info.max_duration) {
      throw new Error("Este corte excede a duração permitida pela conta TikTok.");
    }
    const toggles = [["allow_comment", "Permitir comentários", !info.comment],
      ["allow_duet", "Permitir duetos", !info.duet], ["allow_stitch", "Permitir costura", !info.stitch]];
    options.push(...toggles.map(([key, label, disabled]) => field(label, input(key, "checkbox", "", {disabled}))));
    const commercial = input("commercial", "checkbox", "");
    const own = input("own_brand", "checkbox", "");
    const branded = input("branded_content", "checkbox", "");
    const promo = node("p", {className: "meta"});
    const disclosures = node("fieldset", {}, node("legend", {}, "Divulgação comercial"), field("Minha marca", own),
      field("Marca de terceiros", branded), promo);
    const policy = node("a", {href: "https://www.tiktok.com/legal/page/global/bc-policy/en",
      target: "_blank", rel: "noopener noreferrer"}, "Política de conteúdo de marca");
    const terms = node("span", {}, "Concordo com a ",
      node("a", {href: "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en",
        target: "_blank", rel: "noopener noreferrer"}, "Confirmação de uso de música"), " do TikTok",
      node("span", {id: "branded-terms"}, " e com a ", policy), ".");
    options.push(field("Ativar divulgação comercial", commercial), disclosures,
      node("label", {className: "consent"}, input("tiktok_terms", "checkbox", "", {required: true}), terms),
      node("p", {className: "meta"}, "O TikTok pode levar alguns minutos para processar o corte."));
    updateDisclosure = () => {
      disclosures.hidden = !commercial.checked;
      if (!commercial.checked) own.checked = branded.checked = false;
      branded.disabled = privacy.value === "SELF_ONLY";
      if (branded.disabled) branded.checked = false;
      const privateOption = Array.from(privacy.options).find((option) => option.value === "SELF_ONLY");
      if (privateOption) privateOption.disabled = branded.checked;
      promo.textContent = branded.checked ? "O vídeo terá o rótulo Parceria paga."
        : own.checked ? "O vídeo terá o rótulo Conteúdo promocional."
        : branded.disabled ? "Parcerias pagas exigem visibilidade elegível." : "Selecione o tipo de divulgação.";
      get("branded-terms").hidden = !branded.checked;
      commercial.setCustomValidity(commercial.checked && !own.checked && !branded.checked
        ? "Selecione um tipo de divulgação comercial." : "");
    };
    for (const control of [commercial, own, branded, privacy]) control.addEventListener("change", updateDisclosure);
  }
  dialog(`${platformNames[account.platform]} · ${account.label}`, [
    node("p", {}, `Versão ${clip.revision} · ${seconds(clip.fields.end - clip.fields.start)}`),
    node("video", {className: "publish-preview", controls: true, playsInline: true, preload: "metadata",
      src: `/api/clips/${clip.id}/video`}),
    ...metadataControls(fields), ...options,
    field("Publicar em (horário local, opcional)", input("due", "datetime-local", "")),
    field("Autorizo enviar este corte para esta conta com as opções acima.", input("consent", "checkbox", "", {required: true})),
  ], "Autorizar publicação", async (form) => {
    const due = text(form, "due");
    const result = await api(`/clips/${clip.id}/publish`, "POST", {
      ...metadata(form), account_id: account.id, revision: clip.revision, due: due ? new Date(due).getTime() / 1000 : null,
      privacy: form.elements.namedItem("privacy")?.value || "", consent: checked(form, "consent"),
      tiktok_terms: checked(form, "tiktok_terms"),
      ...Object.fromEntries(["allow_comment", "allow_duet", "allow_stitch", "commercial", "own_brand", "branded_content"]
        .map((key) => [key, checked(form, key)])),
    });
    flash(`Publicação · ${labels[result.state] || result.state}.`);
    viewTo("publishing");
  });
  if (updateDisclosure) updateDisclosure();
}

const metricsFields = {views: "Visualizações", likes: "Curtidas", comments: "Comentários",
  shares: "Compartilhamentos", retention: "Retenção (%)"};

function showMetrics(metrics) {
  return Object.entries(metricsFields).filter(([key]) => metrics[key] !== null)
    .map(([key, title]) => `${title} ${Number(metrics[key]).toLocaleString("pt-BR")}`).join(" · ");
}

async function metricsDialog(entry) {
  const history = await api(`/publications/${entry.id}/metrics`);
  const fields = Object.entries(metricsFields).map(([key, title]) =>
    field(title, input(key, "number", "", {min: 0, ...(key === "retention" ? {max: 100, step: "any"} : {step: 1})})));
  dialog("Métricas da publicação", [
    node("p", {}, "Informe os números exibidos no painel da plataforma."),
    row(...fields),
    ...(history.length ? [node("details", {}, node("summary", {}, "Medições anteriores"),
      ...history.map((item) => node("p", {}, `${date(item.recorded)} · ${showMetrics(item.fields)}`)))] : []),
  ], "Registrar métricas", async (form) => {
    const payload = Object.fromEntries(Object.keys(metricsFields)
      .map((key) => [key, text(form, key) === "" ? null : number(form, key)]));
    await api(`/publications/${entry.id}/metrics`, "POST", payload);
  });
}

function publishingRows() {
  const platforms = Object.entries(platformNames).map(([key, name]) => {
    const accounts = library.accounts.filter((account) => account.platform === key);
    const status = library.integrations[key] ? "OAuth configurado" : "Integração indisponível nesta instalação";
    return node("article", {className: "platform"}, node("h3", {}, name), node("p", {className: "meta"}, status),
      ...accounts.map((account) => node("p", {}, account.label)),
      library.integrations[key] ? button("Conectar conta", async () => {
        const result = await api(`/oauth/${key}/start`, "POST");
        location.assign(result.url);
      }) : node("p", {className: "meta"}, "Baixe o MP4 na aba Cortes para postar manualmente."));
  });
  get("platforms").replaceChildren(...platforms);
  renderList("publications", library.publications.map((entry) => {
    const clip = library.clips.find((c) => c.id === entry.clip_id);
    let link = null;
    if (entry.url) {
      try {
        const url = new URL(entry.url);
        if (url.protocol === "https:") link = node("a", {href: url.href, rel: "noopener noreferrer", target: "_blank"}, "Abrir publicação");
      } catch { /* Preserve the publication history for malformed links. */ }
    }
    const statistics = library.metrics.find((metric) => metric.publication_id === entry.id);
    const actions = node("div", {className: "actions"}, entry.state === "PENDING"
      ? node("span", {className: "badge"}, "A autorizar") : badge(entry.state));
    if (entry.state === "PUBLISHED") actions.append(button("Registrar métricas", () => metricsDialog(entry)));
    if (["APPROVED", "SCHEDULED"].includes(entry.state)) actions.append(button("Retirar autorização", async () => {
      await api(`/publications/${entry.id}/cancel`, "POST");
      await refresh();
    }));
    return node("article", {className: "item"}, node("div", {className: "item-main"},
      node("h3", {}, clip?.fields.title || "Corte"),
      node("p", {className: "meta"}, `${platformNames[entry.platform]} · ${entry.label} · v${entry.revision} · ${date(entry.due)}`),
      statistics ? node("p", {}, showMetrics(statistics.fields)) : null,
      entry.error ? node("p", {}, entry.error) : null, link), actions);
  }), "Nenhuma publicação registrada.");
}

function syncVisible(next) {
  const changed = (keys) => keys.some((key) => JSON.stringify(next[key]) !== JSON.stringify(library[key]));
  const updates = [changed(["videos", "jobs"]), changed(["jobs", "clips", "videos"]),
                   changed(["clips", "jobs", "accounts"]), changed(["publications", "accounts", "integrations", "clips", "metrics"])];
  library = next;
  if (updates[0]) videoRows();
  if (updates[1]) jobRows();
  if (updates[2]) renderClips();
  if (updates[3]) publishingRows();
}

async function refresh() {
  try {
    syncVisible(await api("/library"));
    get("service").textContent = "Conectado";
    get("service").classList.remove("offline");
  } catch {
    get("service").textContent = "Servidor indisponível";
    get("service").classList.add("offline");
  }
}

function sendVideo(file, progress, status) {
  const extension = /\.(mp4|webm)$/i.exec(file.name);
  if (!extension) return Promise.reject(new Error("Selecione um arquivo MP4 ou WebM."));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/videos?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", `video/${extension[1].toLowerCase()}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        progress.value = Math.round(event.loaded / event.total * 100);
        status.textContent = `Enviando · ${progress.value}%`;
      }
    };
    xhr.upload.onload = () => { status.textContent = "Verificando o arquivo de vídeo…"; };
    xhr.onerror = () => reject(new Error("A conexão foi interrompida durante o upload."));
    xhr.onload = () => {
      let result = {};
      try { result = JSON.parse(xhr.responseText); } catch { /* Respond with a generic message below. */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else reject(new Error(responseError(result, `Falha no upload (${xhr.status}).`)));
    };
    xhr.send(file);
  });
}

document.querySelectorAll(".nav").forEach((el) => el.addEventListener("click", () => viewTo(el.dataset.view)));
get("review-filter").addEventListener("change", renderClips);
get("close-dialog").addEventListener("click", () => get("dialog").close());
get("upload").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button");
  const progress = get("upload-progress");
  const status = get("upload-status");
  const file = get("source").files[0];
  submit.disabled = true;
  progress.hidden = false;
  progress.value = 0;
  status.hidden = false;
  status.textContent = "Preparando envio…";
  try {
    await sendVideo(file, progress, status);
    form.reset();
    status.textContent = "Gravação recebida. Selecione Processar para iniciar a análise.";
    await refresh();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
    progress.hidden = true;
  }
});
const oauth = new URLSearchParams(location.search).get("oauth");
if (oauth) {
  viewTo("publishing");
  flash(oauth === "connected" ? "Conta conectada." : "A conexão OAuth não foi autorizada.", oauth !== "connected");
  history.replaceState({}, "", "/");
}
videoRows();
jobRows();
renderClips();
publishingRows();
async function poll() {
  await refresh();
  setTimeout(poll, 2500);
}
void poll();
