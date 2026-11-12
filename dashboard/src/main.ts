import { createApp } from 'vue'
import { createPinia } from 'pinia'
import vuetify from '@/plugins/vuetify'
import router from '@/router'
import App from '@/App.vue'
import { reveal } from '@/directives/reveal'
import api from '@/api/client'
import { watchConnection } from '@/api/connection-watch'

import '@mdi/font/css/materialdesignicons.css'
import '@/styles/tokens.css'
import '@/styles/motion.css'
import '@/styles/tap-targets.css'

const pinia = createPinia()
const app = createApp(App).use(vuetify).use(router).use(pinia).directive('reveal', reveal)

// Failed /api calls (axios and fetch) feed the connection banner. Installed
// before mount, so the very first requests of a view are covered.
watchConnection(api)

app.mount('#app')
