import { createApp } from 'vue'
import { createPinia } from 'pinia'
import vuetify from '@/plugins/vuetify'
import router from '@/router'
import App from '@/App.vue'
import { reveal } from '@/directives/reveal'

import '@mdi/font/css/materialdesignicons.css'
import '@/styles/tokens.css'
import '@/styles/motion.css'

createApp(App)
  .use(vuetify)
  .use(router)
  .use(createPinia())
  .directive('reveal', reveal)
  .mount('#app')
