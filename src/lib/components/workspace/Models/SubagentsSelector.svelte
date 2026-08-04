<script lang="ts">
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import TypeaheadSelector from './TypeaheadSelector.svelte';
	import { getContext } from 'svelte';

	type Subagent = {
		id: string;
		handle?: string;
		name?: string;
		description?: string;
		is_active?: boolean;
		meta?: { remote?: { enabled?: boolean }; [key: string]: any };
	};

	export let subagents: Subagent[] = [];
	export let selectedSubagentIds: string[] = [];

	const i18n = getContext('i18n') as any;

	$: activeSubagents = subagents.filter((subagent) => subagent.is_active !== false);
	$: selectedSubagents = activeSubagents.filter((subagent) =>
		selectedSubagentIds.includes(subagent.id)
	);

	const toggleSubagent = (subagent: Subagent) => {
		selectedSubagentIds = selectedSubagentIds.includes(subagent.id)
			? selectedSubagentIds.filter((id) => id !== subagent.id)
			: [...selectedSubagentIds, subagent.id];
	};
</script>

<div>
	<div class="flex w-full items-center gap-2 mb-1">
		<div class=" self-center text-xs text-gray-500">{$i18n.t('Subagents')}</div>

		{#if activeSubagents.length > 0}
			<TypeaheadSelector
				id="model-subagents-selector"
				items={activeSubagents}
				selectedIds={selectedSubagentIds}
				placeholder={$i18n.t('Search subagents')}
				triggerLabel={$i18n.t('Select Subagent')}
				emptyLabel={$i18n.t('No subagents found')}
				variant="dropdown"
				on:select={(e) => {
					toggleSubagent(e.detail);
				}}
				on:enableall={(e) => {
					selectedSubagentIds = [
						...new Set([...selectedSubagentIds, ...e.detail.map((subagent) => subagent.id)])
					];
				}}
			/>
		{/if}
	</div>

	<div class="flex flex-col mb-1">
		{#if activeSubagents.length > 0}
			<div class=" flex items-center flex-wrap mt-1">
				{#each selectedSubagents as subagent}
					<div class=" flex items-center gap-2 mr-3">
						<div class="self-center flex items-center">
							<Checkbox
								state="checked"
								on:change={(e) => {
									if (e.detail === 'unchecked') {
										selectedSubagentIds = selectedSubagentIds.filter((id) => id !== subagent.id);
									}
								}}
							/>
						</div>

						<Tooltip content={subagent.description ?? subagent.handle ?? subagent.name}>
							<div class="py-0.5 text-xs flex items-center gap-1.5">
								<span class="capitalize">{subagent.name}</span>
								{#if subagent.handle}
									<span class="text-gray-400 dark:text-gray-500">{subagent.handle}</span>
								{/if}
								{#if subagent.meta?.remote?.enabled}
									<span class="text-gray-400 dark:text-gray-500">{$i18n.t('Remote')}</span>
								{/if}
							</div>
						</Tooltip>
					</div>
				{/each}

				{#if selectedSubagents.length > 0}
					<button
						type="button"
						class="py-0.5 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
						on:click={() => {
							selectedSubagentIds = [];
						}}
					>
						{$i18n.t('Disable all')}
					</button>
				{/if}
			</div>
		{/if}
	</div>

	<div class=" text-xs dark:text-gray-700">
		{$i18n.t('To select subagents here, add them to the "Subagents" workspace first.')}
	</div>
</div>
