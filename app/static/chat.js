// Opening chat: shows the bot's questions and sends the answers to the API.
(function () {
  const root = document.querySelector("[data-date-id]");
  const dateId = root.dataset.dateId;
  const messagesEl = document.getElementById("messages");
  const optionsEl = document.getElementById("options");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const doneEl = document.getElementById("done");

  function render(view) {
    messagesEl.replaceChildren(
      ...view.messages.map((m) => {
        const div = document.createElement("div");
        div.className = "message " + m.role;
        div.textContent = m.content; // textContent: never inject HTML
        return div;
      })
    );
    optionsEl.replaceChildren(
      ...view.options.map((label) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        b.addEventListener("click", () => send(label));
        return b;
      })
    );
    form.hidden = view.done;
    doneEl.hidden = !view.done;
    messagesEl.lastElementChild?.scrollIntoView({ block: "end" });
  }

  async function send(text) {
    if (!text.trim()) return;
    form.querySelector("button").disabled = true;
    input.value = "";
    try {
      const res = await fetch(`/api/dates/${dateId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (res.ok) render(await res.json());
      else alert("Something went wrong (" + res.status + "). Please try again.");
    } finally {
      form.querySelector("button").disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });

  fetch(`/api/dates/${dateId}/chat`).then((r) => r.json()).then(render);
})();
