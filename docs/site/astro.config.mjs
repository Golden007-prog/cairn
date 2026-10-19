import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightImageZoom from 'starlight-image-zoom';
import astroMermaid from 'astro-mermaid';

// The user docs, served at https://docs.cairndev.sh. The site lives in the
// public repository as docs/site (ADR-0121 D4): a docs fix and the code change
// it describes land in one pull request.
//
// Content lives at src/content/docs/ and maps 1:1 to routes at the domain
// root (/installation/, /features/pipelines/, ...).
//
// Docs editorial rule: document ONLY features already built and verified in
// this repository — no roadmap, no "coming soon". A feature that is part of
// cairn-platform is named only to say so.
export default defineConfig({
  site: 'https://docs.cairndev.sh',
  // Pages folded into the feature pages (#2668) keep their old URLs working.
  redirects: {
    '/pipelines': '/features/pipelines/',
    '/governance': '/features/governance/',
    '/triggers': '/features/triggers-and-events/',
    '/runtimes': '/features/backends/',
    '/database': '/features/backends/',
    '/services': '/features/models-and-agents/',
    '/modelops': '/features/training/',
    '/ask': '/',
    '/console': '/',
  },
  markdown: {
    shikiConfig: {
      excludeLangs: ['mermaid'],
    },
  },
  integrations: [
    astroMermaid(),
    starlight({
      plugins: [starlightImageZoom()],
      title: 'cairn',
      logo: { src: './src/assets/logo.svg' },
      description:
        'A self-hosted engine for governed pipelines — data, agents, models and operations behind gates, budgets and an audit trail.',
      customCss: ['./src/styles/custom.css'],
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/cairndev-sh/cairn',
        },
      ],
      // Sidebar lists only pages that exist and are truthful.
      sidebar: [
        // Cross-link to the marketing landing — a separate deployment.
        { label: '← Home', link: 'https://cairndev.sh', attrs: { target: '_self' } },
        {
          label: 'Start',
          items: [
            { label: 'Overview', slug: '' },
            { label: 'Installation', slug: 'installation' },
            { label: 'Quickstart', slug: 'quickstart' },
            { label: 'Architecture', slug: 'architecture' },
          ],
        },
        {
          label: 'Concepts',
          items: [
            { label: 'Packs', slug: 'concepts/packs' },
            { label: 'Operators', slug: 'concepts/operators' },
            { label: 'Profiles', slug: 'concepts/profiles-environment' },
            { label: 'Runs & provenance', slug: 'concepts/runs-provenance' },
            { label: 'Configuration', slug: 'configuration' },
          ],
        },
        {
          label: 'Features',
          items: [
            { label: 'All features (proof status)', slug: 'features' },
            { label: 'Pipelines', slug: 'features/pipelines' },
            { label: 'Gates', slug: 'features/gates' },
            { label: 'Dataflow & artifacts', slug: 'features/dataflow-and-artifacts' },
            { label: 'Governance', slug: 'features/governance' },
            { label: 'Triggers & events', slug: 'features/triggers-and-events' },
            { label: 'Models & agents', slug: 'features/models-and-agents' },
            { label: 'Monitor & run hooks', slug: 'features/monitor-and-hooks' },
            { label: 'Training', slug: 'features/training' },
            { label: 'Remote execution & secrets', slug: 'features/remote-and-secrets' },
            { label: 'Backends', slug: 'features/backends' },
            { label: 'Recovery', slug: 'features/recovery' },
            { label: 'Workers', slug: 'features/workers' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'Checked data to an assistant', slug: 'guides/data-to-assistant' },
            { label: 'A nightly job', slug: 'guides/nightly-ops' },
            { label: 'A governed fine-tune', slug: 'guides/governed-finetune' },
            { label: 'Surviving a crash', slug: 'guides/recovery' },
          ],
        },
        {
          label: 'Operate',
          items: [
            { label: 'Deployment', slug: 'deployment' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'CLI', slug: 'reference/cli' },
            { label: 'Builtin operators', slug: 'reference/operators' },
          ],
        },
      ],
    }),
  ],
});
