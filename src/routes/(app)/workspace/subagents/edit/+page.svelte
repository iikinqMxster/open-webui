<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { goto } from '$app/navigation';
	import { subagents } from '$lib/stores';
	import { onMount, getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { getSubagentById, getSubagents, updateSubagentById } from '$lib/apis/subagents';
	import { page } from '$app/stores';

	import SubagentEditor from '$lib/components/workspace/Subagents/SubagentEditor.svelte';

	let subagent = null;
	let disabled = false;

	$: subagentId = $page.url.searchParams.get('id');

	const onSubmit = async (_subagent) => {
		const updatedSubagent = await updateSubagentById(localStorage.token, subagentId, _subagent).catch(
			(error) => {
				toast.error(`${error}`);
				return null;
			}
		);

		if (updatedSubagent) {
			toast.success($i18n.t('Subagent updated successfully'));
			await subagents.set(await getSubagents(localStorage.token));
			subagent = {
				id: updatedSubagent.id,
				handle: updatedSubagent.handle,
				name: updatedSubagent.name,
				description: updatedSubagent.description,
				base_model_id: updatedSubagent.base_model_id ?? null,
				params: updatedSubagent.params ?? {},
				meta: updatedSubagent.meta ?? {},
				is_active: updatedSubagent.is_active,
				access_grants:
					updatedSubagent?.access_grants === undefined ? [] : updatedSubagent?.access_grants
			};
		}
	};

	onMount(async () => {
		if (subagentId) {
			const _subagent = await getSubagentById(localStorage.token, subagentId).catch((error) => {
				toast.error(`${error}`);
				return null;
			});

			if (_subagent) {
				disabled = !_subagent.write_access ?? true;
				subagent = {
					id: _subagent.id,
					handle: _subagent.handle,
					name: _subagent.name,
					description: _subagent.description,
					base_model_id: _subagent.base_model_id ?? null,
					params: _subagent.params ?? {},
					meta: _subagent.meta ?? {},
					is_active: _subagent.is_active,
					access_grants: _subagent?.access_grants === undefined ? [] : _subagent?.access_grants
				};
			} else {
				goto('/workspace/subagents');
			}
		} else {
			goto('/workspace/subagents');
		}
	});
</script>

{#if subagent}
	<SubagentEditor {subagent} {onSubmit} {disabled} edit />
{/if}
