<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { goto } from '$app/navigation';
	import { subagents } from '$lib/stores';
	import { onMount, getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { createNewSubagent, getSubagents } from '$lib/apis/subagents';
	import SubagentEditor from '$lib/components/workspace/Subagents/SubagentEditor.svelte';

	let subagent = null;

	const onSubmit = async (_subagent) => {
		const res = await createNewSubagent(localStorage.token, _subagent).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			toast.success($i18n.t('Subagent created successfully'));
			await subagents.set(await getSubagents(localStorage.token));
			await goto('/workspace/subagents');
		}
	};

	onMount(async () => {
		if (sessionStorage.subagent) {
			const _subagent = JSON.parse(sessionStorage.subagent);
			sessionStorage.removeItem('subagent');

			subagent = {
				name: _subagent.name || 'Subagent',
				id: '', // clone gets a fresh internal UUID from the backend
				handle: _subagent.handle || '',
				description: _subagent.description || '',
				base_model_id: _subagent.base_model_id ?? null,
				params: _subagent.params ?? {},
				meta: _subagent.meta ?? {},
				is_active: _subagent.is_active ?? true,
				access_grants: _subagent.access_grants !== undefined ? _subagent.access_grants : []
			};
		}
	});
</script>

{#key subagent}
	<SubagentEditor {subagent} {onSubmit} />
{/key}
