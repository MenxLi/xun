// Shows agent activity in the tab title, so progress is visible without
// switching to this tab. Status prefixes are symbols rather than words to stay
// language-neutral; the title is only touched while the tab is hidden.

const baseTitle = document.title

let running = false
let waiting = false

function apply() {
  const status = document.hidden ? (waiting ? '○ ' : running ? '● ' : '') : ''
  document.title = status ? `${status}${baseTitle}` : baseTitle
}

document.addEventListener('visibilitychange', apply)

export function setTabActivity(state: { running: boolean; waiting: boolean }) {
  [running, waiting] = [state.running, state.waiting]
  apply()
}
